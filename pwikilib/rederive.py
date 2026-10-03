"""저장된 행에 지금 규칙(가림·분류·카드)을 다시 적용한다. 원천 파일을 다시 읽지 않는다(원천은 30일 정리로 지워진다).

쓰임
- pwiki rederive            미리보기. DB 를 읽기 전용(mode=ro)으로 열고 쓰지 않는다. 바뀔 행 수·가림 종류·카드 증감·
                            바뀌는 행 안의 알려진 값 독립 대조(전 → 후)를 보인다.
- pwiki rederive --apply    적용. 수집 잠금 아래 한 트랜잭션으로 고친다. 백업 사본은 만들지 않는다(적용 전 내용은
                            가림이 덜 된 사본이 되기 때문이다). 트랜잭션이라 중간에 실패하면 그대로 되돌아간다.
- 소급 가림(수집이 부른다)  새로 수확한 값·바뀐 secrets.local 값의 닻(영숫자 연속)이나 새 약한 값 해시 앞자리가 든
                            저장 행만 골라 같은 처리를 한다. 늦게 수확된 값이 앞서 저장한 행·FTS·카드에 남지 않게 한다.
- 저장 행 수확             미리보기·적용과, 규칙 판이 바뀐 뒤 첫 수집이 먼저 한다. 저장 행(events.raw, docs)에서 지금
                            규칙으로 수확해 목록에 올리고 같은 실행에서 그 값으로 가린다. 옛 규칙이 문맥으로 못 잡던 값은
                            수확되지 않아 문맥 없는 사본이 남는데, 원천은 30일 뒤 지워지므로 저장 행이 유일한 회수 경로다.

행마다 하는 일
- events: raw(가림·정제 뒤 객체)를 지금 Redactor 로 다시 가리고 classify·extract 로 kind·글·도구 입력·에러 줄·압축 절·
  FTS 본문을 다시 뽑는다. 달라진 행만 고친다(가림은 멱등: 이미 토큰인 자리는 다시 잡지 않는다).
- docs: 글은 글로, meta 는 JSON 을 풀어 다시 가리고 FTS 본문을 다시 넣는다.
- cards·sessions: 바뀐 세션의 카드를 다시 만든다. 전체 적용은 모든 세션을 다시 만든다(카드 규칙이 바뀐 것도 반영).
값은 출력하지 않는다(개수와 키만).
"""
import collections
import json
import os
import re
import time

from . import cards as cards_mod
from . import db as dbm
from . import leakscan
from . import parse
from . import paths
from .redact import (Harvested, Redactor, _anchors, candidate_forms, digests, hash_prefixes, load_known_values,
                     value_strength)

# 저장 행을 만든 규칙의 판. 가림·분류·카드 규칙을 바꾸면 올린다. 수집은 DB 판이 다르면 경고만 하고(무거운 전체 적용을
# 주기 수집 안에서 몰래 돌리지 않는다), pwiki rederive --apply 가 적용 뒤 이 판을 적는다.
DERIVE_VERSION = "2026-10-03.2"  # .1 에 더해: => 구분자, HTML 따옴표, %253D·&amp;#61;· , 값의 두 겹 인코딩
# 저장 행 수확을 마친 규칙 판. 규칙이 바뀌면 지금 규칙으로 새로 문맥이 잡히는 값이 저장 행에 있을 수 있다(옛 규칙은 그 자리를
# 가리지 않았고 수확도 안 했다). 수집은 이 판이 코드 판과 다르면 저장 행을 한 번 수확해 소급 가림을 하고 이 판을 적는다.
STORED_HARVEST_KEY = "stored_harvest_version"
DOC_CAP = {"memory": None, "wf_run": 8000, "wf_script": 6000, "tool_output": 6000, "agent_meta": 1000}
_HEX_RUN = re.compile(r"[0-9a-f]{6,128}")


def secrets_sig():
    """secrets.local 의 크기·수정 시각(내용이 아닌 메타데이터만). 바뀌면 그 값들로 소급 가림을 한다."""
    try:
        st = os.stat(paths.secrets_path())
    except FileNotFoundError:
        return "none"
    return "%d:%d" % (st.st_size, st.st_mtime_ns)


def build_redactor(known, harv):
    """가림 값 = secrets.local + 강한 수확값(지금 규칙으로 비밀 모양인 것), 약한 수확값은 해시."""
    return Redactor(known + [v for v in harv.active_strong() if v not in known], weak_digests=harv.weak)


def harvest_stored(con, H=None):
    """저장 행(events.raw, docs.text·meta)에서 지금 수확 규칙으로 비밀번호·토큰 문맥의 값을 모은다(메모리에서만).
    저장 행은 이미 가려져 있어 옛 규칙이 잡은 자리는 토큰이다(토큰 자리는 수확하지 않는다). 그래서 모이는 값은 지금 규칙이
    새로 문맥으로 잡는 자리의 값이다. {값: 규칙}, 통계."""
    from .ingest import harvest_trigger
    H = H or Redactor([])
    found = {}
    st = collections.Counter()
    t0 = time.time()
    cur = con.execute("SELECT raw FROM events")
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        for (raw,) in rows:
            st["rows"] += 1
            if not raw or not harvest_trigger(raw):
                continue
            st["trigger_rows"] += 1
            try:
                H.harvest_obj(json.loads(raw), found)
            except (TypeError, ValueError):
                H.harvest_text(raw, found)
    for text, meta in con.execute("SELECT text, meta FROM docs"):
        st["docs"] += 1
        if text and harvest_trigger(text):
            st["trigger_docs"] += 1
            H.harvest_text(text, found)
        if meta and harvest_trigger(meta):
            try:
                H.harvest_obj(json.loads(meta), found)
            except (TypeError, ValueError):
                H.harvest_text(meta, found)
    st["candidates"] = len(found)
    st["seconds"] = round(time.time() - t0, 1)
    return found, st


def stored_harvest(con, harv, known):
    """저장 행 수확 결과를 harv(수확 목록, 메모리)에 올린다. 저장은 부르는 쪽이 한다.
    돌려주는 소급 대상은 새로 올라간 값만이 아니라 찾은 값 전부(강한 값, 약한 값 해시)다:
    수확 파일은 저장됐는데 소급 가림 전에 멈췄다면 다음 실행에서 그 값은 '새 값'이 아니지만 저장 행에 아직 남아 있다.
    (강한 값 목록, 약한 값 해시 목록, 통계)"""
    found, st = harvest_stored(con)
    excl = set(known)
    s0, w0 = len(harv.new_strong), len(harv.new_weak)
    vals, digs = [], []
    for raw in found:
        harv.add_raw(raw, exclude=excl)
        for f in candidate_forms(raw):
            s = value_strength(f)
            if f in excl or s is None:
                continue
            if s == "strong":
                if f not in vals:
                    vals.append(f)
            else:
                digs.append(digests(f))
    st["strong_new"] = len(harv.new_strong) - s0
    st["weak_new"] = len(harv.new_weak) - w0
    st["retro_values"] = len(vals)
    st["retro_weak"] = len(digs)
    return vals, digs, st


def make_filter(values, digest_lists):
    """소급 대상 행 고르기: 값의 닻(가림 사전검사와 같은 영숫자 연속, 변형·줄 분할에도 한쪽은 남는다)이나
    약한 값 해시 앞자리(6자 이상 16진 연속)가 든 문자열."""
    anchors = sorted({a for v in values for a in _anchors(v)}, key=len, reverse=True)
    hexes = hash_prefixes([d for ds in digest_lists for d in ds]) if digest_lists else set()

    def hit(s):
        if not s:
            return False
        if any(a in s for a in anchors):
            return True
        if hexes:
            for m in _HEX_RUN.finditer(s):
                if m.group(0) in hexes:
                    return True
        return False
    return hit, len(anchors)


class Plan:
    def __init__(self):
        self.ev = []        # (key, sid, project, ts, nkind, nsub, new_obj, x)
        self.docs = []      # (path, kind, project, sid, mtime, new_text, new_meta_json)
        self.stat = collections.Counter()
        self.moves = collections.Counter()
        self.touched = set()
        self.leak_before = 0
        self.leak_after = 0


def _leak(values, probes, *parts):
    n = 0
    for p in parts:
        if p:
            n += sum(leakscan.count_in_bytes(p, values, probes).values())
    return n


def scan(con, R, row_filter=None, leak_values=None, plan=None):
    """바뀔 행을 계산만 한다(쓰지 않는다). row_filter 가 있으면 그 검사를 통과한 행만 다시 가린다."""
    plan = plan or Plan()
    probes = leakscan.make_probes(leak_values) if leak_values else None
    cur = con.execute("SELECT key, sid, kind, sub, text, raw, project, ts FROM events")
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        for key, sid, kind, sub, text, raw, project, ts in rows:
            plan.stat["events"] += 1
            if row_filter is not None and not row_filter(raw):
                continue
            plan.stat["events_checked"] += 1
            try:
                old = json.loads(raw)
            except (TypeError, ValueError):
                plan.stat["raw_unparsable"] += 1
                continue
            new = R.obj(old, R.scope(raw))
            nkind, nsub = (kind, sub) if kind == "unknown" else parse.classify(new)
            red_changed = new != old
            kind_changed = (nkind, nsub) != (kind, sub)
            if not red_changed and not kind_changed:
                continue
            x = parse.extract(new, nkind, nsub)
            if red_changed:
                plan.stat["events_redaction_changed"] += 1
            if kind_changed:
                plan.stat["events_kind_changed"] += 1
                plan.moves["%s/%s -> %s/%s" % (kind, sub, nkind, nsub)] += 1
            plan.stat["events_changed"] += 1
            plan.ev.append((key, sid, project, ts, nkind, nsub, new, x))
            if sid:
                plan.touched.add(sid)
            if probes is not None:
                # 이 행에서 나온 FTS 본문·도구 입력 행도 함께 센다(적용 때 같이 다시 쓴다)
                fb = con.execute("SELECT body FROM fts WHERE rowid=(SELECT rid FROM fts_map WHERE key=?)", (key,)).fetchone()
                tus = [r[0] for r in con.execute("SELECT inp FROM tool_uses WHERE ekey=?", (key,))]
                nb = parse.fts_body(nkind, nsub, x["text"])
                plan.leak_before += _leak(leak_values, probes, raw, text, fb[0] if fb else None, *tus)
                plan.leak_after += _leak(leak_values, probes, json.dumps(new, ensure_ascii=False), x["text"],
                                         nb if fb else None, *[tu["inp"] for tu in x["tool_uses"]])
    for path, kind, project, sid, text, meta, mtime in con.execute(
            "SELECT path, kind, project, sid, text, meta, mtime FROM docs"):
        plan.stat["docs"] += 1
        if row_filter is not None and not (row_filter(text) or row_filter(meta)):
            continue
        plan.stat["docs_checked"] += 1
        sc = R.scope(text or "")
        nt = R.text(R.text(text, sc), sc) if text else text
        try:
            m_old = json.loads(meta) if meta else None
        except ValueError:
            m_old = None
        m_new = R.obj(m_old, sc) if m_old is not None else None
        if nt == text and m_new == m_old:
            continue
        plan.stat["docs_changed"] += 1
        nm = json.dumps(m_new, ensure_ascii=False) if m_new is not None else meta
        plan.docs.append((path, kind, project, sid, mtime, nt, nm))
        if sid:
            plan.touched.add(sid)
        if probes is not None:
            plan.leak_before += _leak(leak_values, probes, text, meta)
            plan.leak_after += _leak(leak_values, probes, nt, nm)
    return plan


def apply(con, plan, rebuild="touched"):
    """계산한 변경을 쓴다. 부르는 쪽이 트랜잭션을 연다. rebuild: 'touched'(바뀐 세션) 또는 'all'(모든 세션)."""
    from .ingest import Ingestor
    ing = Ingestor(con, None, paths.claude_dir())
    for key, sid, project, ts, nkind, nsub, new, x in plan.ev:
        ncwd = new.get("cwd") if isinstance(new.get("cwd"), str) else None
        con.execute("UPDATE events SET kind=?, sub=?, cwd=?, text=?, raw=?, msg_id=?, model=? WHERE key=?",
                    (nkind, nsub, ncwd, x["text"], json.dumps(new, ensure_ascii=False, separators=(",", ":")),
                     x["msg_id"], x["model"], key))
        for tu in x["tool_uses"]:
            if tu["tuid"]:
                con.execute("UPDATE tool_uses SET name=?, fpath=?, cmd=?, inp=? WHERE tuid=? AND ekey=?",
                            (tu["name"], tu["fpath"], tu["cmd"], tu["inp"], tu["tuid"], key))
        for tr in x["tool_results"]:
            if tr["tuid"]:
                con.execute("UPDATE tool_results SET is_err=?, err_sig=?, err_line=?, denial=?, intr=?, link=?"
                            " WHERE tuid=? AND ekey=?",
                            (tr["is_err"], tr["err_sig"], tr["err_line"], tr["denial"], tr["intr"], tr["link"],
                             tr["tuid"], key))
        con.execute("DELETE FROM compact_sections WHERE ekey=?", (key,))
        for s in x["sections"] or []:
            con.execute("INSERT OR REPLACE INTO compact_sections(ekey, n, title, body, fmt) VALUES (?,?,?,?,?)",
                        (key, s["n"], s["title"], s["body"], s["fmt"]))
        body = parse.fts_body(nkind, nsub, x["text"])
        if nkind == "history" and body and ing._history_dup(sid, x["text"]):
            body = None
        ing.fts_put(key, body, nkind if nkind != "att" else "att:" + str(nsub), project, ts, sid)
    for path, kind, project, sid, mtime, nt, nm in plan.docs:
        con.execute("UPDATE docs SET text=?, meta=? WHERE path=?", (nt, nm, path))
        cap = DOC_CAP.get(kind)
        body = nt if cap is None else (nt or "")[:cap]
        ing.fts_put("d:" + path, body, "doc:" + str(kind), project,
                    paths.to_utc_iso(mtime * 1000) if mtime else None, sid)
    if rebuild == "all":
        sids = [r[0] for r in con.execute("SELECT DISTINCT sid FROM files WHERE kind='session' AND sid IS NOT NULL")]
        sids += [r[0] for r in con.execute("SELECT sid FROM sessions")]
    else:
        sids = list(plan.touched)
    n_cards = 0
    sids = sorted({s for s in sids if s and s not in ("-", "__history__")})
    for sid in sids:
        n_cards += cards_mod.build_session(con, sid)
    return {"sessions": len(sids), "cards": n_cards}


def card_diff(con):
    """모든 세션의 카드를 지금 규칙으로 계산해(쓰지 않음) 저장된 카드와 견준다."""
    before = collections.Counter(r[0] for r in con.execute("SELECT human_kind FROM cards"))
    have = {r[0] for r in con.execute("SELECT key FROM cards")}
    after = collections.Counter()
    keys = set()
    for (sid,) in con.execute("SELECT DISTINCT sid FROM files WHERE kind='session' AND sid IS NOT NULL").fetchall():
        cs, _ = cards_mod.compute_session(con, sid)
        for c in cs or []:
            after[c["human_kind"]] += 1
            keys.add(c["key"])
    delta = {k: after[k] - before[k] for k in set(before) | set(after) if after[k] != before[k]}
    return {"before": sum(before.values()), "after": sum(after.values()), "delta_by_kind": delta,
            "keys_added": len(keys - have), "keys_removed": len(have - keys)}


def run(apply_changes=False):
    """전체 적용(또는 미리보기). 요약 dict 를 돌려준다."""
    t0 = time.time()
    warn = []
    known = load_known_values(warn=warn)
    harv = Harvested().load(warn)
    if not apply_changes:
        con = dbm.connect_ro()
        try:
            # 저장 행 수확(메모리에서만, 수확 파일에 쓰지 않는다): 지금 규칙이 새로 잡는 문맥의 값으로 미리보기도 가린다
            _, _, hst = stored_harvest(con, harv, known)
            R = build_redactor(known, harv)
            leak_values = list(known) + [v for v in harv.active_strong() if v not in known]
            plan = scan(con, R, leak_values=leak_values)
            cd = card_diff(con)
            ver = dbm.get_state(con, "derive_version")
        finally:
            con.close()
        return _summary(plan, R, warn, t0, applied=False, cards=cd, db_version=ver, harvest=hst)
    from .ingest import _lock
    fd = _lock()
    try:
        con = dbm.open_db()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        ver = dbm.get_state(con, "derive_version")
        _, _, hst = stored_harvest(con, harv, known)
        # 수확 파일은 적용 트랜잭션보다 먼저 저장한다: 적용이 실패해도 값은 목록에 남고, 다시 돌린 적용은 목록 전체로 가린다.
        harv.save()
        R = build_redactor(known, harv)
        leak_values = list(known) + [v for v in harv.active_strong() if v not in known]
        plan = scan(con, R, leak_values=leak_values)
        before = con.execute("SELECT count(*) FROM cards").fetchone()[0]
        con.execute("BEGIN IMMEDIATE")
        try:
            rb = apply(con, plan, rebuild="all")
            dbm.set_state(con, "derive_version", DERIVE_VERSION)
            dbm.set_state(con, "secrets_local_sig", secrets_sig())
            dbm.set_state(con, STORED_HARVEST_KEY, DERIVE_VERSION)
            s = _summary(plan, R, warn, t0, applied=True, db_version=ver, harvest=hst,
                         cards={"before": before, "after": rb["cards"], "sessions_rebuilt": rb["sessions"]})
            con.execute("INSERT OR REPLACE INTO runs(at, cmd, summary) VALUES (?,?,?)",
                        (paths.now_utc_iso(), "rederive --apply", json.dumps(
                            {k: v for k, v in s.items() if k != "changed_keys"}, ensure_ascii=False)))
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        s["elapsed_s"] = round(time.time() - t0, 1)
        return s
    finally:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def retro(con, R, values, digest_lists):
    """소급 가림(수집이 부른다, 수집 잠금 안). 새 값의 닻이 든 저장 행만 다시 가리고 바뀐 세션 카드를 다시 만든다."""
    t0 = time.time()
    hit, n_anchor = make_filter(values, digest_lists)
    plan = scan(con, R, row_filter=hit)
    con.execute("BEGIN IMMEDIATE")
    try:
        rb = apply(con, plan, rebuild="touched")
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    out = dict(plan.stat)
    out.update({"values": len(values), "weak_hashes": len(digest_lists), "anchors": n_anchor,
                "sessions_rebuilt": rb["sessions"], "cards_rebuilt": rb["cards"],
                "seconds": round(time.time() - t0, 1)})
    return out


def _summary(plan, R, warn, t0, applied, cards=None, db_version=None, harvest=None):
    return {"applied": applied, "stat": dict(plan.stat), "kind_moves": dict(plan.moves),
            "harvest": dict(harvest or {}),
            "redactions": dict(R.counts), "sessions_touched": len(plan.touched),
            "changed_keys": [e[0] for e in plan.ev[:20]] + ["d:" + d[0] for d in plan.docs[:10]],
            "leak_in_changed": {"before": plan.leak_before, "after": plan.leak_after},
            "cards": cards, "db_version": db_version, "code_version": DERIVE_VERSION, "warnings": warn,
            "elapsed_s": round(time.time() - t0, 1)}


def format_run(s):
    st = s["stat"]
    out = ["# rederive %s" % ("적용" if s["applied"] else "미리보기(읽기 전용, 쓰지 않음)"),
           "규칙 판: DB %s · 코드 %s" % (s["db_version"] or "(기록 없음)", s["code_version"]),
           "events %s행 중 바뀜 %s(가림 %s · 분류 %s) · 원문 JSON 못 읽음 %s · docs %s개 중 바뀜 %s · 바뀐 세션 %d" % (
               tuple("{:,}".format(st.get(k, 0)) for k in ("events", "events_changed", "events_redaction_changed",
                                                         "events_kind_changed", "raw_unparsable", "docs",
                                                         "docs_changed")) + (s["sessions_touched"],))]
    hv = s.get("harvest") or {}
    out.append("- 저장 행 수확(지금 규칙, 값은 출력하지 않음): 훑은 events %s(사전검사 통과 %s) · docs %s · 후보 %s ·"
               " 새 강한 값 %s · 새 약한 값 해시 %s%s · %s초" % (
                   "{:,}".format(hv.get("rows", 0)), "{:,}".format(hv.get("trigger_rows", 0)),
                   "{:,}".format(hv.get("docs", 0)), hv.get("candidates", 0), hv.get("strong_new", 0),
                   hv.get("weak_new", 0), "" if s["applied"] else "(미리보기: 수확 파일에 쓰지 않음)", hv.get("seconds", 0)))
    for k, n in sorted(s["kind_moves"].items(), key=lambda kv: -kv[1]):
        out.append("- 분류 이동 %s: %d" % (k, n))
    out.append("- 새로 가린 횟수(종류별): %s" % (", ".join("%s %d" % kv for kv in sorted(s["redactions"].items(),
                                                                                key=lambda kv: -kv[1])) or "없음"))
    lk = s["leak_in_changed"]
    out.append("- 바뀌는 행(events 원문·글, 그 행의 FTS 본문·도구 입력, docs) 안의 알려진 값·수확값 독립 대조"
               "(leakscan, \\u·이스케이프 변형 포함): 전 %d → 후 %d"
               % (lk["before"], lk["after"]))
    c = s.get("cards") or {}
    if s["applied"]:
        out.append("- 카드: 적용 전 %s장 → 다시 만든 세션 %s개 · 카드 %s장" % (
            "{:,}".format(c.get("before", 0)), c.get("sessions_rebuilt", 0), "{:,}".format(c.get("after", 0))))
    elif c:
        out.append("- 카드(지금 규칙으로 계산만): 저장 %s장 → 계산 %s장 · 새 키 %d · 없어질 키 %d · 종류별 증감 %s" % (
            "{:,}".format(c["before"]), "{:,}".format(c["after"]), c["keys_added"], c["keys_removed"],
            ", ".join("%s %+d" % kv for kv in sorted(c["delta_by_kind"].items())) or "없음"))
    if s["changed_keys"]:
        out.append("- 바뀌는 행 키(앞 30개): " + ", ".join(s["changed_keys"]))
    if s["warnings"]:
        out.append("경고: " + "; ".join(s["warnings"]))
    if not s["applied"]:
        out.append("적용하려면: pwiki rederive --apply (수집 잠금 아래 한 트랜잭션, 백업 사본 없음)")
    out.append("소요 %.1f초" % s["elapsed_s"])
    return "\n".join(out)
