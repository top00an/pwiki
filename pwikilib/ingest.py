"""수집기. ~/.claude 아래 원천을 바이트 오프셋으로 이어 읽어 DB 에 적재한다. DB 에 쓰는 주체는 이것 하나다.

원천
- 세션 기록 <프로젝트>/<세션>.jsonl, 서브에이전트 <세션>/subagents/**/agent-*.jsonl,
  워크플로 저널 <세션>/subagents/workflows/*/journal.jsonl, ~/.claude/history.jsonl  (덧붙이기: 오프셋)
- 메모리 md, 워크플로 스크립트·실행 기록 json, agent meta json, tool-results 파일      (해시 비교: 전체 재읽기)

멱등: 같은 원천을 다시 읽으면 새 행 0. 파일이 줄었거나 inode·앞부분 해시가 바뀌면 처음부터 읽고 키로 중복을 거른다.
"""
import collections
import errno
import fcntl
import hashlib
import json
import os
import re
import time

from . import cards as cards_mod
from . import db as dbm
from . import parse
from . import paths
from . import rederive as rederive_mod
from .redact import Harvested, Redactor, load_known_values

CHUNK = 16 << 20
HEAD_MAX = 65536


class Busy(Exception):
    pass


def fid_of(rel):
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:12]


def project_from_cwd(cwd):
    if not cwd:
        return None
    return re.sub(r"[/\\.:\s]", "-", cwd)


# ---- 원천 찾기 -------------------------------------------------------------------
def discover(claude, project=None, unrec=None):
    """(상대경로, 종류, 프로젝트, 세션id, 방식) 목록. 방식은 'jsonl' 또는 'doc'."""
    out = []
    pdir = os.path.join(claude, "projects")
    unrec = unrec if unrec is not None else collections.Counter()
    if os.path.isdir(pdir):
        for proj in sorted(os.listdir(pdir)):
            if project and proj != project:
                continue
            pp = os.path.join(pdir, proj)
            if not os.path.isdir(pp):
                continue
            for name in sorted(os.listdir(pp)):
                ap = os.path.join(pp, name)
                rel = os.path.relpath(ap, claude)
                if os.path.isfile(ap):
                    if name.endswith(".jsonl"):
                        out.append((rel, "session", proj, name[:-6], "jsonl"))
                    else:
                        unrec["project/*" + (os.path.splitext(name)[1] or name)] += 1
                    continue
                if name == "memory":
                    for dp, dn, fn in os.walk(ap):
                        # 숨은 폴더(.git·.history 등)는 원천이 아니다. 폴더 단위로 개수만 센다.
                        for d in [d for d in dn if d.startswith(".")]:
                            n = sum(len(x[2]) for x in os.walk(os.path.join(dp, d)))
                            unrec["memory/" + d + "/"] += n
                        dn[:] = sorted(d for d in dn if not d.startswith("."))
                        for f in sorted(fn):
                            r = os.path.relpath(os.path.join(dp, f), claude)
                            if f.endswith(".md"):
                                out.append((r, "memory", proj, None, "doc"))
                            else:
                                unrec["memory/*" + (os.path.splitext(f)[1] or "(확장자 없음)")] += 1
                    continue
                sid = name
                for dp, dn, fn in os.walk(ap):
                    dn.sort()
                    reldir = os.path.relpath(dp, ap)
                    parts = [] if reldir == "." else reldir.split(os.sep)
                    for f in sorted(fn):
                        r = os.path.relpath(os.path.join(dp, f), claude)
                        if parts and parts[0] == "subagents":
                            if f.startswith("agent-") and f.endswith(".jsonl"):
                                k = "wf_agent" if len(parts) >= 3 and parts[1] == "workflows" else "subagent"
                                out.append((r, k, proj, sid, "jsonl"))
                            elif f == "journal.jsonl" or (f.startswith("journal") and f.endswith(".jsonl")):
                                out.append((r, "journal", proj, sid, "jsonl"))
                            elif f.startswith("agent-") and f.endswith(".meta.json"):
                                out.append((r, "agent_meta", proj, sid, "doc"))
                            else:
                                unrec["subagents/" + (os.path.splitext(f)[1] or f)] += 1
                        elif parts and parts[0] == "workflows":
                            if len(parts) >= 2 and parts[1] == "scripts" and f.endswith(".js"):
                                out.append((r, "wf_script", proj, sid, "doc"))
                            elif len(parts) == 1 and f.startswith("wf_") and f.endswith(".json"):
                                out.append((r, "wf_run", proj, sid, "doc"))
                            else:
                                unrec["workflows/" + (os.path.splitext(f)[1] or f)] += 1
                        elif parts and parts[0] == "tool-results":
                            out.append((r, "tool_output", proj, sid, "doc"))
                        else:
                            unrec["session_dir/" + (os.path.splitext(f)[1] or f)] += 1
    if not project:
        hp = os.path.join(claude, "history.jsonl")
        if os.path.isfile(hp):
            out.append(("history.jsonl", "history", None, None, "jsonl"))
    return out


# ---- 적재 -----------------------------------------------------------------------
class Stats:
    def __init__(self):
        self.c = collections.Counter()
        self.excl = collections.Counter()
        self.strip_n = collections.Counter()
        self.strip_b = collections.Counter()
        self.touched = set()
        self.kinds = collections.Counter()
        self.unrec = collections.Counter()
        self.fields = collections.Counter()
        self.unknown = collections.Counter()
        self.harvest = collections.Counter()
        self.changed = []
        self.retro = None

    def as_dict(self):
        return {
            "counts": dict(self.c), "excluded": dict(self.excl), "unknown_kept": dict(self.unknown),
            "harvest": dict(self.harvest),
            "strips": {k: [self.strip_n[k], self.strip_b[k]] for k in self.strip_n},
            "kinds": dict(self.kinds), "unrecognized": dict(self.unrec),
            "unknown_fields": dict(self.fields),
            "changed_files": self.changed,
        }


def _head_sha(path, n):
    with open(path, "rb") as fh:
        return hashlib.sha1(fh.read(n)).hexdigest()


def plan_start(con, rel, ap, st):
    """(files 행, 처음부터 다시 읽는지, 읽기 시작 오프셋). inode 가 바뀌었거나 파일이 줄었거나 앞부분 해시가 다르면 처음부터."""
    row = con.execute("SELECT inode, size, off, head_len, head_sha1, n_lines, n_uuid, n_bad, resets "
                      "FROM files WHERE path=?", (rel,)).fetchone()
    reset = False
    if row is None:
        return row, False, 0
    inode, size, off, head_len, head_sha1 = row[0], row[1], row[2], row[3], row[4]
    if inode != st.st_ino or st.st_size < off:
        reset = True
    elif head_len and st.st_size >= head_len and _head_sha(ap, head_len) != head_sha1:
        reset = True
    return row, reset, (0 if reset else off)


def iter_lines(ap, start, size):
    """start 부터 size 까지에서 개행으로 끝난 줄만 (줄 바이트, 오프셋) 으로 낸다. 마지막 개행 뒤는 확정하지 않는다."""
    pos = start
    with open(ap, "rb") as fh:
        fh.seek(start)
        remaining = size - start
        buf = b""
        while remaining > 0:
            chunk = fh.read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            buf += chunk
            last = buf.rfind(b"\n")
            if last < 0:
                continue
            block = buf[:last + 1]
            buf = buf[last + 1:]
            i = 0
            while i < len(block):
                j = block.index(b"\n", i)
                yield block[i:j], pos + i
                i = j + 1
            pos += len(block)


# 수확 사전검사: 값 그룹이 있는 규칙의 사전검사 낱말이 줄 원문(바이트)에 있을 때만 JSON 을 풀어 수확한다.
def _harvest_triggers():
    from .redact import HARVEST_KINDS, PATTERNS
    ci, cs = [], []
    for kind, pat, pre, mode, low in PATTERNS:
        if kind in HARVEST_KINDS and pre:
            (ci if low else cs).extend(p.encode("utf-8") for p in pre)
    cs.append(b"apikey_")
    return tuple(sorted(set(ci))), tuple(sorted(set(cs)))


_TRIG_CI, _TRIG_CS = _harvest_triggers()
_TRIG_CI_S = tuple(p.decode("utf-8") for p in _TRIG_CI)
_TRIG_CS_S = tuple(p.decode("utf-8") for p in _TRIG_CS)


def harvest_trigger(raw):
    """줄 원문(bytes) 또는 저장된 글(str)에 수확 규칙의 사전검사 낱말이 있는가."""
    ci, cs = (_TRIG_CI_S, _TRIG_CS_S) if isinstance(raw, str) else (_TRIG_CI, _TRIG_CS)
    if any(p in raw for p in cs):
        return True
    low = raw.lower()
    return any(p in low for p in ci)


def harvest_pass(con, claude, srcs, H, found, stats):
    """가림 전에, 이번 실행에서 새로 읽을 바이트(줄 파일)와 바뀐 문서에서 비밀번호·토큰 문맥의 값을 모은다.
    모은 값은 이번 실행의 가림(알려진 값)에 들어가므로, 같은 값이 다른 문맥에 먼저 나와도 가려진다."""
    for rel, kind, proj, sid, how in srcs:
        ap = os.path.join(claude, rel)
        try:
            st = os.stat(ap)
        except FileNotFoundError:
            continue
        if how == "jsonl":
            row, reset, start = plan_start(con, rel, ap, st)
            if st.st_size <= start:
                continue
            for raw, off in iter_lines(ap, start, st.st_size):
                stats["harvest_lines"] += 1
                if not harvest_trigger(raw):
                    continue
                stats["harvest_trigger_lines"] += 1
                try:
                    o = json.loads(raw)
                except ValueError:
                    continue
                H.harvest_obj(o, found)
        else:
            with open(ap, "rb") as fh:
                data = fh.read()
            sha = hashlib.sha1(data).hexdigest()
            r = con.execute("SELECT sha1 FROM docs WHERE path=?", (rel,)).fetchone()
            if r is not None and r[0] == sha:
                continue
            text = data.decode("utf-8", errors="replace")
            try:
                H.harvest_obj(json.loads(text), found)
            except ValueError:
                H.harvest_text(text, found)


class Ingestor:
    def __init__(self, con, redactor, claude):
        self.con = con
        self.R = redactor
        self.claude = claude
        self.st = Stats()
        self.proj_by_sid = {}
        self.proj_by_cwd = {}

    # -- FTS ---------------------------------------------------------------
    def fts_put(self, key, body, kind, project, ts, sid):
        self.fts_del(key)
        if not body:
            return
        cur = self.con.execute("INSERT INTO fts(body, key, kind, project, ts, sid) VALUES (?,?,?,?,?,?)",
                               (body, key, kind, project, ts, sid))
        self.con.execute("INSERT OR REPLACE INTO fts_map(key, rid) VALUES (?,?)", (key, cur.lastrowid))

    def fts_del(self, key):
        r = self.con.execute("SELECT rid FROM fts_map WHERE key=?", (key,)).fetchone()
        if r:
            self.con.execute("DELETE FROM fts WHERE rowid=?", (r[0],))
            self.con.execute("DELETE FROM fts_map WHERE key=?", (key,))

    # -- jsonl ---------------------------------------------------------------
    def ingest_jsonl(self, rel, kind, project, sid_hint):
        con = self.con
        ap = os.path.join(self.claude, rel)
        fid = fid_of(rel)
        try:
            st = os.stat(ap)
        except FileNotFoundError:
            return
        row, reset, start = plan_start(con, rel, ap, st)
        self.st.kinds[kind] += 1
        if row is not None and not reset and st.st_size == start:
            return
        con.execute("BEGIN IMMEDIATE")
        try:
            now = paths.now_utc_iso()
            if row is None:
                con.execute("INSERT INTO files(path, fid, kind, project, sid, inode, dev, size, mtime, off, first_seen) "
                            "VALUES (?,?,?,?,?,?,?,?,?,0,?)",
                            (rel, fid, kind, project, sid_hint, st.st_ino, st.st_dev, st.st_size, st.st_mtime, now))
                n_lines = n_uuid = n_bad = 0
                resets = 0
            else:
                n_lines, n_uuid, n_bad, resets = row[5], row[6], row[7], row[8]
            ls_have = collections.Counter()
            if reset:
                resets += 1
                self.st.c["files_reset"] += 1
                con.execute("DELETE FROM excl WHERE fid=?", (fid,))
                con.execute("DELETE FROM strips WHERE fid=?", (fid,))
                n_lines = n_uuid = n_bad = 0
                for lsha, n in con.execute("SELECT lsha, count(*) FROM events WHERE fid=? AND uuid IS NULL "
                                           "GROUP BY lsha", (fid,)):
                    ls_have[lsha] = n
            ctx = {"fid": fid, "rel": rel, "kind": kind, "project": project, "sid_hint": sid_hint,
                   "reset": reset, "ls_have": ls_have, "ls_seen": collections.Counter(), "seen": set(),
                   "excl": collections.Counter(), "excl_b": collections.Counter(),
                   "strips": {}, "n_lines": 0, "n_uuid": 0, "n_bad": 0, "stored": 0}
            pos = start
            for raw, off in iter_lines(ap, start, st.st_size):
                self._line(raw, off, ctx)
                pos = off + len(raw) + 1
            for (reason, hu), n in ctx["excl"].items():
                con.execute("INSERT INTO excl(fid, reason, has_uuid, n, bytes) VALUES (?,?,?,?,?) "
                            "ON CONFLICT(fid, reason, has_uuid) DO UPDATE SET n=n+excluded.n, bytes=bytes+excluded.bytes",
                            (fid, reason, hu, n, ctx["excl_b"][(reason, hu)]))
                self.st.excl[reason] += n
            for reason, (n, b) in ctx["strips"].items():
                con.execute("INSERT INTO strips(fid, reason, n, bytes) VALUES (?,?,?,?) "
                            "ON CONFLICT(fid, reason) DO UPDATE SET n=n+excluded.n, bytes=bytes+excluded.bytes",
                            (fid, reason, n, b))
                self.st.strip_n[reason] += n
                self.st.strip_b[reason] += b
            head_len = min(pos, HEAD_MAX)
            head_sha1 = _head_sha(ap, head_len) if head_len else None
            con.execute("UPDATE files SET inode=?, dev=?, size=?, mtime=?, off=?, head_len=?, head_sha1=?, "
                        "n_lines=?, n_uuid=?, n_bad=?, resets=?, missing=0, last_ingest=?, kind=?, project=?, sid=? "
                        "WHERE path=?",
                        (st.st_ino, st.st_dev, st.st_size, st.st_mtime, pos, head_len, head_sha1,
                         n_lines + ctx["n_lines"], n_uuid + ctx["n_uuid"], n_bad + ctx["n_bad"], resets, now,
                         kind, project, sid_hint, rel))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        self.st.c["lines_read"] += ctx["n_lines"]
        self.st.c["rows_new"] += ctx["stored"]
        self.st.c["bad_json"] += ctx["n_bad"]
        if ctx["n_lines"]:
            self.st.c["files_changed"] += 1
            self.st.changed.append([rel, ctx["n_lines"], ctx["stored"], "reset" if reset else "append"])
        if ctx["stored"] and kind in ("session", "subagent", "wf_agent", "journal"):
            self.st.touched.add(sid_hint)
        if ctx["stored"] and kind == "history":
            self.st.touched.add("__history__")

    def _excl(self, ctx, reason, has_uuid, nbytes):
        ctx["excl"][(reason, 1 if has_uuid else 0)] += 1
        ctx["excl_b"][(reason, 1 if has_uuid else 0)] += nbytes

    def _line(self, raw, off, ctx):
        ctx["n_lines"] += 1
        if not raw.strip():
            self._excl(ctx, "blank", False, len(raw))
            return
        try:
            o = json.loads(raw)
        except ValueError:
            ctx["n_bad"] += 1
            self._excl(ctx, "bad_json", False, len(raw))
            return
        if not isinstance(o, dict):
            self._excl(ctx, "not_object", False, len(raw))
            return
        uuid = o.get("uuid") if isinstance(o.get("uuid"), str) and o.get("uuid") else None
        if uuid:
            ctx["n_uuid"] += 1
        if ctx["kind"] == "history":
            o = dict(o)
            o["type"] = "history"
        keep, reason = parse.line_policy(o)
        if not keep:
            self._excl(ctx, reason, uuid, len(raw))
            return
        unknown = reason  # 모르는 type·첨부: 가린 뒤 kind=unknown 으로 적재한다
        for uf in parse.unknown_fields(o):
            self.st.fields[uf] += 1
        sid = o.get("sessionId") if isinstance(o.get("sessionId"), str) else None
        sid = sid or ctx["sid_hint"] or "-"
        lsha = None
        con = self.con
        if uuid:
            key = "%s:%s" % (sid, uuid)
            if key in ctx["seen"]:
                self._excl(ctx, "dup_key", uuid, len(raw))
                return
            ctx["seen"].add(key)
            ex = con.execute("SELECT fid FROM events WHERE key=?", (key,)).fetchone()
            if ex is not None:
                # 처음부터 다시 읽는 중이면 같은 파일의 이미 적재한 줄이다(적재 수에 이미 들어 있다).
                if ex[0] != ctx["fid"]:
                    self._excl(ctx, "dup_key_other_file", uuid, len(raw))
                elif not ctx["reset"]:
                    self._excl(ctx, "dup_key", uuid, len(raw))
                return
        else:
            lsha = parse.line_sha(raw)
            key = "%s:o:%s:%d:%s" % (sid, ctx["fid"], off, lsha[:8])
            if ctx["reset"]:
                # uuid 없는 줄은 오프셋이 키에 들어가므로, 다시 읽을 때는 같은 내용의 줄 개수로 중복을 거른다.
                ctx["ls_seen"][lsha] += 1
                if ctx["ls_seen"][lsha] <= ctx["ls_have"].get(lsha, 0):
                    return
            ex = con.execute("SELECT fid FROM events WHERE key=?", (key,)).fetchone()
            if ex is not None:
                self._excl(ctx, "dup_key" if ex[0] == ctx["fid"] else "dup_key_other_file", uuid, len(raw))
                return
        red = self.R.obj(parse.reduce_line(o, ctx["strips"]), self.R.scope(raw))
        if unknown:
            kind, sub = "unknown", unknown.split(":", 1)[0].replace("unknown_", "") + ":" + unknown.split(":", 1)[1]
            self.st.c["unknown_kept"] += 1
            self.st.unknown[unknown] += 1
        else:
            kind, sub = parse.classify(red)
        x = parse.extract(red, kind, sub)
        ts = paths.to_utc_iso(o.get("timestamp"))
        if ctx["kind"] == "history":
            project = self.proj_by_sid.get(sid) or self.proj_by_cwd.get(o.get("project")) \
                or project_from_cwd(o.get("project"))
        else:
            project = ctx["project"]
        u = x["usage"] or (None, None, None, None)
        con.execute(
            "INSERT INTO events(key, sid, uuid, parent, fid, off, type, sub, kind, ts, side, agent, project, cwd, ver, pid,"
            " entry, msg_id, model, u_in, u_out, u_cc, u_cr, text, raw, lsha) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, sid, uuid, o.get("parentUuid") if isinstance(o.get("parentUuid"), str) else None,
             ctx["fid"], off, o.get("type"), sub, kind, ts, 1 if o.get("isSidechain") else 0,
             o.get("agentId") if isinstance(o.get("agentId"), str) else None, project,
             red.get("cwd") if isinstance(red.get("cwd"), str) else None,
             o.get("version") if isinstance(o.get("version"), str) else None,
             o.get("promptId") if isinstance(o.get("promptId"), str) else None,
             o.get("entrypoint") if isinstance(o.get("entrypoint"), str) else None,
             x["msg_id"], x["model"], u[0], u[1], u[2], u[3], x["text"],
             json.dumps(red, ensure_ascii=False, separators=(",", ":")), lsha))
        ctx["stored"] += 1
        self.st.c["kind:" + kind] += 1
        for tu in x["tool_uses"]:
            if tu["tuid"]:
                con.execute("INSERT OR IGNORE INTO tool_uses(tuid, ekey, sid, fid, off, idx, name, fpath, cmd, inp, ts) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (tu["tuid"], key, sid, ctx["fid"], off, tu["idx"], tu["name"], tu["fpath"], tu["cmd"],
                             tu["inp"], ts))
        for tr in x["tool_results"]:
            if tr["tuid"]:
                con.execute("INSERT OR IGNORE INTO tool_results(tuid, ekey, sid, fid, off, is_err, err_sig, err_line,"
                            " denial, intr, link, ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            (tr["tuid"], key, sid, ctx["fid"], off, tr["is_err"], tr["err_sig"], tr["err_line"],
                             tr["denial"], tr["intr"], tr["link"], ts))
        if x["sections"]:
            for s in x["sections"]:
                con.execute("INSERT OR REPLACE INTO compact_sections(ekey, n, title, body, fmt) VALUES (?,?,?,?,?)",
                            (key, s["n"], s["title"], s["body"], s["fmt"]))
        body = parse.fts_body(kind, sub, x["text"])
        if kind == "history" and body:
            if self._history_dup(sid, x["text"]):
                body = None
        if body:
            self.fts_put(key, body, kind if kind != "att" else "att:" + str(sub), project, ts, sid)

    def _history_dup(self, sid, text):
        head = " ".join((text or "").split())[:60]
        if not head:
            return True
        for (t,) in self.con.execute("SELECT text FROM events WHERE sid=? AND kind IN ('human','sdk','queued','qo_human',"
                                     "'bash_in','slash') AND fid IN (SELECT fid FROM files WHERE sid=? AND kind='session')",
                                     (sid, sid)):
            if " ".join((t or "").split())[:60] == head:
                return True
        return False

    # -- 문서(덧붙이기 아님) ----------------------------------------------------
    def ingest_doc(self, rel, kind, project, sid):
        con = self.con
        ap = os.path.join(self.claude, rel)
        try:
            st = os.stat(ap)
            with open(ap, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            return
        self.st.kinds[kind] += 1
        sha = hashlib.sha1(data).hexdigest()
        row = con.execute("SELECT sha1 FROM docs WHERE path=?", (rel,)).fetchone()
        if row is not None and row[0] == sha:
            if st.st_mtime:
                pass
            return
        text = data.decode("utf-8", errors="replace")
        scope = self.R.scope(data)
        meta = {}
        j = None
        if kind in ("agent_meta", "wf_run"):
            try:
                j = json.loads(text)
            except ValueError:
                j = None
        if isinstance(j, dict):
            # JSON 문서는 푼 객체째 가린다. 다시 직렬화한 글(\n 이 리터럴)에 가림을 걸면 줄 머리 경계 규칙이 빗나간다.
            j = self.R.obj(j, scope)
            if kind == "agent_meta":
                base = os.path.basename(rel)
                meta = {k: j.get(k) for k in ("agentType", "description", "toolUseId", "workflowPhase", "model",
                                              "spawnDepth", "requestShape")}
                meta["agentId"] = base[len("agent-"):-len(".meta.json")]
                text = json.dumps(j, ensure_ascii=False, indent=1)
            else:
                meta = {k: j.get(k) for k in ("runId", "workflowName", "summary", "status", "totalTokens",
                                              "totalToolCalls", "agentCount", "durationMs", "taskId", "timestamp",
                                              "defaultModel")}
                res = j.get("result")
                text = "\n".join(str(x) for x in (
                    j.get("workflowName"), j.get("summary"),
                    json.dumps(res, ensure_ascii=False) if res is not None else "",
                    json.dumps(j.get("phases"), ensure_ascii=False) if j.get("phases") else "") if x)
        else:
            text = self.R.text(text, scope)
        # 두 번째 가림(멱등): 직렬화로 생긴 글에 남은 형태가 있으면 여기서 잡는다.
        text = self.R.text(text, scope)
        meta = self.R.obj(meta, scope)
        now = paths.now_utc_iso()
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute("INSERT INTO docs(path, kind, project, sid, sha1, size, mtime, text, meta, missing, first_seen, updated)"
                        " VALUES (?,?,?,?,?,?,?,?,?,0,?,?) ON CONFLICT(path) DO UPDATE SET kind=excluded.kind,"
                        " project=excluded.project, sid=excluded.sid, sha1=excluded.sha1, size=excluded.size,"
                        " mtime=excluded.mtime, text=excluded.text, meta=excluded.meta, missing=0, updated=excluded.updated",
                        (rel, kind, project, sid, sha, len(data), st.st_mtime, text,
                         json.dumps(meta, ensure_ascii=False), now, now))
            from .rederive import DOC_CAP
            cap = DOC_CAP.get(kind)
            body = text if cap is None else text[:cap]
            ts = paths.to_utc_iso(st.st_mtime * 1000)
            self.fts_put("d:" + rel, body, "doc:" + kind, project, ts, sid)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        self.st.c["docs_new" if row is None else "docs_changed"] += 1
        if sid:
            self.st.touched.add(sid)

    def mark_missing(self, seen_rels, project=None):
        con = self.con
        q = "SELECT path FROM files WHERE missing=0" + (" AND project=?" if project else "")
        gone = [r[0] for r in con.execute(q, (project,) if project else ()) if r[0] not in seen_rels]
        q2 = "SELECT path FROM docs WHERE missing=0" + (" AND project=?" if project else "")
        gone_d = [r[0] for r in con.execute(q2, (project,) if project else ()) if r[0] not in seen_rels]
        if gone or gone_d:
            con.execute("BEGIN IMMEDIATE")
            for p in gone:
                con.execute("UPDATE files SET missing=1 WHERE path=?", (p,))
            for p in gone_d:
                con.execute("UPDATE docs SET missing=1 WHERE path=?", (p,))
            con.execute("COMMIT")
        self.st.c["files_missing_now"] = len(gone)
        self.st.c["docs_missing_now"] = len(gone_d)


def _lock():
    paths.ensure_home()
    fd = os.open(paths.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
            raise Busy("다른 pwiki ingest 가 실행 중이다(잠금 %s)" % paths.lock_path())
        raise
    os.ftruncate(fd, 0)
    os.write(fd, str(os.getpid()).encode())
    return fd


def run(project=None, log=None):
    """적재 1회. 요약 dict 를 돌려준다."""
    t0 = time.time()
    fd = _lock()
    try:
        con = dbm.open_db()
        warn = []
        known = load_known_values(warn=warn)
        claude = paths.claude_dir()
        unrec = collections.Counter()
        srcs = discover(claude, project, unrec)
        harv = Harvested().load(warn)
        n_before = con.execute("SELECT (SELECT count(*) FROM events) + (SELECT count(*) FROM docs)").fetchone()[0]
        # 0) 저장 행 수확: 규칙 판이 바뀐 뒤 처음 한 번. 지금 규칙이 새로 문맥으로 잡는 값을 저장 행에서 모아 목록에 올리고,
        #    그 값이 든 저장 행을 소급 가림한다(옛 규칙이 수확하지 않아 문맥 없는 사본이 남아 있을 수 있다).
        #    수확 파일은 소급보다 먼저 저장한다(이 단계는 새 바이트 수확 전이라 저장분은 저장 행에서 찾은 값뿐이다).
        #    소급 전에 멈추면 판을 적지 않았으므로 다음 실행이 다시 수확하고, 찾은 값 전부(새 값이 아니어도)로 소급한다.
        stored = None
        if not n_before:
            dbm.set_state(con, rederive_mod.STORED_HARVEST_KEY, rederive_mod.DERIVE_VERSION)
        elif dbm.get_state(con, rederive_mod.STORED_HARVEST_KEY) != rederive_mod.DERIVE_VERSION:
            sv, sd, stored = rederive_mod.stored_harvest(con, harv, known)
            harv.save()
            if sv or sd:
                stored["retro"] = rederive_mod.retro(con, rederive_mod.build_redactor(known, harv), sv, sd)
            dbm.set_state(con, rederive_mod.STORED_HARVEST_KEY, rederive_mod.DERIVE_VERSION)
            harv.new_strong, harv.new_weak = [], []  # 이 값들의 소급은 끝났다(아래 1단계의 새 값과 섞지 않는다)
        # 1) 수확: 이번에 읽을 새 바이트와 바뀐 문서에서 비밀번호·토큰 문맥의 값을 먼저 모은다(가림 전, 메모리에서).
        hstats = collections.Counter()
        found = {}
        th = time.time()
        harvest_pass(con, claude, srcs, Redactor([]), found, hstats)
        for v in found:
            harv.add_raw(v, exclude=set(known))
        hstats["candidates"] = len(found)
        hstats["strong_new"] = len(harv.new_strong)
        hstats["weak_new"] = len(harv.new_weak)
        hstats["strong_total"] = len(harv.strong)
        hstats["strong_active"] = len(harv.active_strong())
        hstats["weak_total"] = len(harv.weak)
        hstats["seconds"] = round(time.time() - th, 1)
        # 2) 가림: 알려진 값 = secrets.local + 강한 수확값(지금 규칙으로 비밀 모양인 것, 평문), 약한 수확값은 해시만.
        R = Redactor(known + [v for v in harv.active_strong() if v not in known], weak_digests=harv.weak)
        # 3) 소급 가림: 새로 수확한 값(또는 바뀐 secrets.local 값)이 앞서 저장한 행에 남지 않게 그 값이 든 행만 다시 가린다.
        #    수확 파일 저장보다 먼저 한다(중간에 멈추면 다음 실행이 같은 값을 다시 새 값으로 보고 소급한다).
        sig = rederive_mod.secrets_sig()
        active = set(harv.active_strong())
        retro_vals = [v for v in harv.new_strong if v not in known and v in active]
        if dbm.get_state(con, "secrets_local_sig") != sig:
            retro_vals = list(known) + retro_vals
        retro = None
        if n_before and (retro_vals or harv.new_weak):
            retro = rederive_mod.retro(con, R, retro_vals, harv.new_weak)
        dbm.set_state(con, "secrets_local_sig", sig)
        harv.save()
        dv = dbm.get_state(con, "derive_version")
        if not n_before:
            dbm.set_state(con, "derive_version", rederive_mod.DERIVE_VERSION)
        elif dv != rederive_mod.DERIVE_VERSION:
            warn.append("저장된 행의 규칙 판(%s)이 코드 판(%s)과 다르다: pwiki rederive 로 미리 보고 pwiki rederive --apply 로"
                        " 저장된 행에 지금 규칙을 다시 적용한다" % (dv or "기록 없음", rederive_mod.DERIVE_VERSION))
        ing = Ingestor(con, R, claude)
        ing.st.unrec.update(unrec)
        ing.st.harvest.update(hstats)
        if retro is not None:
            ing.st.retro = retro
        # 세션 파일을 먼저 읽고 history 를 마지막에 읽는다(history 의 프로젝트·중복 판정에 세션을 쓴다).
        order = {"session": 0, "subagent": 1, "wf_agent": 1, "journal": 2, "history": 9}
        jl = sorted([s for s in srcs if s[4] == "jsonl"], key=lambda s: (order.get(s[1], 5), s[0]))
        for rel, kind, proj, sid, _ in jl:
            if kind == "history":
                for s, p in con.execute("SELECT sid, project FROM files WHERE kind='session'"):
                    ing.proj_by_sid[s] = p
                for cwd, p in con.execute("SELECT cwd, project FROM events WHERE kind IN ('human','sdk') "
                                          "AND cwd IS NOT NULL GROUP BY cwd, project"):
                    ing.proj_by_cwd.setdefault(cwd, p)
            ing.ingest_jsonl(rel, kind, proj, sid)
        for rel, kind, proj, sid, _ in [s for s in srcs if s[4] == "doc"]:
            ing.ingest_doc(rel, kind, proj, sid)
        ing.mark_missing({s[0] for s in srcs}, project)
        touched = sorted(s for s in ing.st.touched if s and s not in ("-", "__history__"))
        nb = cards_mod.build_sessions(con, touched)
        ing.st.c["sessions_rebuilt"] = nb["sessions"]
        ing.st.c["cards_built"] = nb["cards"]
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        acc = accounting(con, project)
        summary = ing.st.as_dict()
        summary["redactions"] = dict(R.counts)
        summary["retro"] = ing.st.retro
        summary["stored_harvest"] = stored
        summary["accounting"] = acc
        summary["warnings"] = warn
        summary["known_values"] = len(known)
        summary["elapsed_s"] = round(time.time() - t0, 1)
        summary["totals"] = totals(con)
        brief = {k: v for k, v in summary.items() if k != "accounting"}
        brief["accounting_mismatch_files"] = acc["mismatch_files"]
        con.execute("INSERT OR REPLACE INTO runs(at, cmd, summary) VALUES (?,?,?)",
                    (paths.now_utc_iso(), "ingest" + (" --project " + project if project else " --all"),
                     json.dumps(brief, ensure_ascii=False)))
        con.close()
        _log(summary)
        return summary
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _log(summary):
    try:
        with open(os.path.join(paths.logs_dir(), "ingest.log"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": paths.now_utc_iso(), "counts": summary.get("counts"),
                                 "mismatch": summary.get("accounting", {}).get("mismatch_files"),
                                 "elapsed_s": summary.get("elapsed_s")}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def accounting(con, project=None):
    """파일마다 '원문 uuid 줄 수 = 적재 uuid 행 + 사유별 제외 uuid 줄' 과 전체 줄 수 식을 확인한다."""
    q = "SELECT fid, path, n_lines, n_uuid FROM files WHERE kind IN ('session','subagent','wf_agent','journal','history')"
    args = ()
    if project:
        q += " AND project=?"
        args = (project,)
    stored_u = dict(con.execute("SELECT fid, count(*) FROM events WHERE uuid IS NOT NULL GROUP BY fid"))
    stored_a = dict(con.execute("SELECT fid, count(*) FROM events GROUP BY fid"))
    ex_u = dict(con.execute("SELECT fid, sum(n) FROM excl WHERE has_uuid=1 GROUP BY fid"))
    ex_a = dict(con.execute("SELECT fid, sum(n) FROM excl GROUP BY fid"))
    n_files = 0
    bad = []
    tot = collections.Counter()
    for fid, path, n_lines, n_uuid in con.execute(q, args):
        n_files += 1
        su, sa, eu, ea = stored_u.get(fid, 0), stored_a.get(fid, 0), ex_u.get(fid, 0) or 0, ex_a.get(fid, 0) or 0
        tot["uuid_lines"] += n_uuid
        tot["stored_uuid"] += su
        tot["excluded_uuid"] += eu
        tot["lines"] += n_lines
        tot["stored_all"] += sa
        tot["excluded_all"] += ea
        if n_uuid != su + eu or n_lines != sa + ea:
            bad.append({"path": path, "uuid_lines": n_uuid, "stored_uuid": su, "excluded_uuid": eu,
                        "lines": n_lines, "stored_all": sa, "excluded_all": ea})
    return {"files": n_files, "mismatch_files": len(bad), "mismatch": bad[:20], "totals": dict(tot)}


def totals(con):
    out = {}
    for name, q in (("events", "SELECT count(*) FROM events"), ("cards", "SELECT count(*) FROM cards"),
                    ("docs", "SELECT count(*) FROM docs"), ("fts_rows", "SELECT count(*) FROM fts_map"),
                    ("files", "SELECT count(*) FROM files"), ("sessions", "SELECT count(*) FROM sessions"),
                    ("tool_uses", "SELECT count(*) FROM tool_uses"), ("tool_results", "SELECT count(*) FROM tool_results")):
        out[name] = con.execute(q).fetchone()[0]
    return out
