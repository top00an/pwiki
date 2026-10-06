"""Obsidian vault export (output only). SQLite is the source of truth.

- days/<YYYY-MM-DD>.md, projects/<project>.md, cards/<YYYY-MM-DD>/<card file>.md, index.md
- If a file's hash differs from the last export hash (a person edited it), it is not overwritten and is reported as a conflict.
- Pages exported before but not produced this time (cards merged or gone) are deleted only if no person edited them
  (otherwise a conflict).
- Human notes live in _notes/. pwiki neither reads nor writes _notes/ (it only creates an intro README once, the first time).
- Before writing, each page body is redaction-checked; a page with remaining secrets is not written.
"""
import collections
import hashlib
import json
import os

from . import paths
from .redact import Checker, Harvested, load_known_values
from .views import _fold_counts, bundle_of, fmt_dur, one_line, project_family, short_project

NOTICE = ("> 이 페이지는 pwiki 가 자동으로 만든다. 여기서 고친 내용은 pwiki 가 다시 읽지 않으며, 고친 파일은 다음 내보내기 때 "
          "덮지 않고 충돌로 보고한다. 사람 메모는 `_notes/` 폴더에 둔다.")


def card_file(key, start_ts):
    parts = key.split(":")
    sid = parts[1] if len(parts) > 1 else "x"
    tail = parts[-1]
    return "c-%s-%s" % (sid[:8], tail[:8])


def _fm(d):
    lines = ["---"]
    for k, v in d.items():
        if isinstance(v, (list, tuple)):
            lines.append("%s: [%s]" % (k, ", ".join(json.dumps(x, ensure_ascii=False) for x in v)))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            lines.append("%s: %s" % (k, v))
        elif v is None:
            lines.append("%s: null" % k)
        else:
            lines.append("%s: %s" % (k, json.dumps(str(v), ensure_ascii=False)))
    lines.append("---")
    return "\n".join(lines)


def _proj_page(p):
    return "p-" + short_project(p).replace("/", "-")


def _quote(text, n=1500):
    t = (text or "").strip()
    if len(t) > n:
        t = t[:n] + "…(잘림)"
    return "\n".join("> " + ln for ln in t.splitlines()) if t else "> (없음)"


def build_pages(con):
    pages = {}
    cards = con.execute(
        "SELECT key, sid, project, cwd, first_uuid, human_kind, delivered, start_ts, end_ts, dur_s, turn_ms, human, last_asst,"
        " n_asst, n_tools, tools, files, n_files, cmds, n_err, err_sigs, n_err_repeat, n_denied, n_intr, tok_in, tok_out,"
        " tok_cc, tok_cr, n_sub, n_wf, sub_tok_all, features, model FROM cards ORDER BY start_ts").fetchall()
    titles = dict(con.execute("SELECT sid, title FROM sessions"))
    by_day = collections.defaultdict(list)
    by_proj = collections.defaultdict(list)
    for c in cards:
        d = paths.kst_date(c[7]) or "unknown"
        by_day[d].append(c)
        by_proj[c[2]].append(c)
        name = card_file(c[0], c[7])
        feats = json.loads(c[31] or "{}")
        fm = _fm(collections.OrderedDict([
            ("type", "card"), ("key", c[0]), ("project", c[2]), ("session", c[1]), ("first_uuid", c[4]),
            ("start_kst", paths.kst_str(c[7], "%Y-%m-%d %H:%M:%S")), ("end_kst", paths.kst_str(c[8], "%Y-%m-%d %H:%M:%S")),
            ("duration_s", round(c[9] or 0, 1)), ("delivered", c[6]), ("input_kind", c[5]),
            ("responses", c[13] or 0), ("tool_calls", c[14] or 0), ("files_changed", c[17] or 0), ("errors", c[19] or 0),
            ("denied", c[22] or 0), ("interrupts", c[23] or 0), ("tokens_out", c[25] or 0),
            ("tokens_all", (c[24] or 0) + (c[25] or 0) + (c[26] or 0) + (c[27] or 0)),
            ("subagents", c[28] or 0), ("workflows", c[29] or 0), ("model", c[32]),
            ("features", [k for k, v in feats.items() if v and k != "permission_modes"]),
        ]))
        body = [fm, "", NOTICE, "", "# %s" % (one_line(c[11], 70) or "(빈 입력)"), "",
                "날짜 [[%s]] · 프로젝트 [[%s|%s]] · 세션 `%s`%s" % (d, _proj_page(c[2]), short_project(c[2]), c[1][:8],
                                                          (" (" + one_line(titles.get(c[1]), 50) + ")") if titles.get(c[1]) else ""),
                "", "## 사람 입력", _quote(c[11]), "", "## 마지막 답(앞부분)", _quote(c[12], 800), ""]
        tools = json.loads(c[15] or "{}")
        body.append("## 도구")
        body.append(", ".join("%s %d" % (k, v) for k, v in tools.items()) if tools else "(없음)")
        files = json.loads(c[16] or "[]")
        body += ["", "## 바뀐 파일 (%d)" % len(files)] + (["- `%s`" % f for f in files[:100]] or ["(없음)"])
        cmds = json.loads(c[18] or "[]")
        body += ["", "## 명령 앞머리"] + (["- `%s`" % x.replace("`", "'") for x in cmds[:20]] or ["(없음)"])
        sigs = json.loads(c[20] or "[]")
        body += ["", "## 에러 (%d, 같은 서명 반복 %d)" % (c[19] or 0, c[21] or 0)]
        body += (["- sig:%s ×%d · %s" % (s, n, one_line(line, 150)) for s, n, line in sigs[:20]] or ["(없음)"])
        body += ["", "## 사실 숫자",
                 "- 소요 %s · 작업 시간(turn_duration 합) %s · 응답 %d · 도구 호출 %d" % (
                     fmt_dur(c[9]), fmt_dur((c[10] or 0) / 1000.0), c[13] or 0, c[14] or 0),
                 "- 토큰 입력 %d · 출력 %d · 캐시 생성 %d · 캐시 읽기 %d · 하위 에이전트 %d" % (
                     c[24] or 0, c[25] or 0, c[26] or 0, c[27] or 0, c[30] or 0),
                 "- 거절 %d · 중단 %d · 서브에이전트 %d · 워크플로 %d" % (c[22] or 0, c[23] or 0, c[28] or 0, c[29] or 0)]
        pages["cards/%s/%s.md" % (d, name)] = "\n".join(body) + "\n"
    for d, cs in by_day.items():
        groups = collections.defaultdict(list)
        for c in cs:
            groups[bundle_of(c[5], c[6])].append(c)
        projs = collections.OrderedDict()
        for c in groups["human"]:
            projs.setdefault(c[2], []).append(c)
        fm = _fm(collections.OrderedDict([("type", "day"), ("date", d), ("human_cards", len(groups["human"])),
                                          ("auto_cards", len(groups["auto"])), ("no_human_cards", len(groups["no_human"])),
                                          ("undelivered_queue", len(groups["ghost"])),
                                          ("projects", [short_project(p) for p in projs])]))
        body = [fm, "", NOTICE, "", "# %s 작업 타임라인 (%s)" % (d, paths.tz_label_for_date(d)), "",
                "사람 입력 카드 %d장 · 프로젝트 %d개 · 따로 묶음: 자동 실행 %d장 · 사람 입력 없음 %d장 · 전달 안 된 대기열 %d건" % (
                    len(groups["human"]), len(projs), len(groups["auto"]), len(groups["no_human"]), len(groups["ghost"]))]

        def link_line(c, with_proj=False):
            return "- %s%s [[%s|%s]] · 소요 %s · 도구 %d · 파일 %d" % (
                paths.kst_str(c[7], "%H:%M"), (" " + short_project(c[2])) if with_proj else "", card_file(c[0], c[7]),
                one_line(c[11], 60).replace("|", "/").replace("]", ")") or "(빈 입력)", fmt_dur(c[9]), c[14] or 0, c[17] or 0)
        for p, pcs in projs.items():
            body += ["", "## [[%s|%s]] · 카드 %d" % (_proj_page(p), short_project(p), len(pcs))]
            body += [link_line(c) for c in pcs]
        if groups["no_human"]:
            body += ["", "## 사람 입력 없음(팀원 세션 등) · %d장" % len(groups["no_human"])]
            body += [link_line(c, True) for c in groups["no_human"]]
        if groups["auto"]:
            body += ["", "## 자동 실행(claude -p) · %d장" % len(groups["auto"]),
                     "- 프로젝트(계열로 접음): " + _fold_counts(c[2] for c in groups["auto"])]
        if groups["ghost"]:
            body += ["", "## 전달 안 된 대기열 · %d건" % len(groups["ghost"])]
            body += [link_line(c, True) for c in groups["ghost"]]
        pages["days/%s.md" % d] = "\n".join(body) + "\n"
    for p, cs in by_proj.items():
        days = collections.Counter(paths.kst_date(c[7]) or "unknown" for c in cs)
        fm = _fm(collections.OrderedDict([("type", "project"), ("project", p), ("cards", len(cs)), ("days", len(days))]))
        body = [fm, "", NOTICE, "", "# %s" % short_project(p), "", "카드 %d장 · 일수 %d" % (len(cs), len(days)), "", "## 날짜"]
        body += ["- [[%s]] · 카드 %d" % (d, n) for d, n in sorted(days.items(), reverse=True)]
        body += ["", "## 최근 카드 30장"]
        for c in list(reversed(cs))[:30]:
            body.append("- %s [[%s|%s]]" % (paths.kst_str(c[7], "%m-%d %H:%M"), card_file(c[0], c[7]),
                                            one_line(c[11], 60).replace("|", "/").replace("]", ")") or "(빈 입력)"))
        pages["projects/%s.md" % _proj_page(p)] = "\n".join(body) + "\n"
    idx = [_fm(collections.OrderedDict([("type", "index"), ("cards", len(cards)), ("days", len(by_day)),
                                        ("projects", len(by_proj))])), "", NOTICE, "", "# pwiki 색인", "", "## 프로젝트"]
    fam = collections.OrderedDict()
    for p, cs in sorted(by_proj.items(), key=lambda kv: -len(kv[1])):
        fam.setdefault(project_family(p), []).append((p, cs))
    for f, members in fam.items():
        if len(members) == 1:
            p, cs = members[0]
            idx.append("- [[%s|%s]] · 카드 %d" % (_proj_page(p), short_project(p), len(cs)))
        else:
            idx.append("- %s · 프로젝트 %d개(계열로 접음) · 카드 %d" % (short_project(f), len(members),
                                                                sum(len(cs) for _, cs in members)))
    idx += ["", "## 최근 날짜"] + ["- [[%s]] · 카드 %d" % (d, len(by_day[d])) for d in sorted(by_day, reverse=True)[:60]]
    pages["index.md"] = "\n".join(idx) + "\n"
    return pages


NOTES_README = ("# 사람 메모\n\n이 폴더는 사람이 쓰는 곳이다. pwiki 는 이 폴더를 읽지도 덮지도 않는다.\n"
                "자동 생성 페이지(days·projects·cards·index)에서 고친 내용도 다시 읽지 않는다.\n")


def export(con, vault=None):
    vault = vault or paths.vault_dir()
    os.makedirs(vault, exist_ok=True)
    notes = os.path.join(vault, "_notes")
    os.makedirs(notes, exist_ok=True)
    rd = os.path.join(notes, "README.md")
    if not os.path.exists(rd):
        with open(rd, "w", encoding="utf-8") as fh:
            fh.write(NOTES_README)
    known = load_known_values()
    chk = Checker(known)
    harvested = [v for v in Harvested().load().active_strong() if v not in known]
    pages = build_pages(con)
    manifest = dict(con.execute("SELECT path, sha1 FROM exports"))
    st = collections.Counter()
    conflicts = []
    blocked = []
    now = paths.now_utc_iso()
    con.execute("BEGIN IMMEDIATE")
    try:
        for rel, text in sorted(pages.items()):
            leak = {}
            chk.count_text(text, leak)
            if leak or any(v in text for v in harvested):
                blocked.append(rel)
                st["blocked_leak"] += 1
                continue
            data = text.encode("utf-8")
            sha = hashlib.sha1(data).hexdigest()
            ap = os.path.join(vault, rel)
            if os.path.exists(ap):
                with open(ap, "rb") as fh:
                    cur = hashlib.sha1(fh.read()).hexdigest()
                last = manifest.get(rel)
                if last is None or cur != last:
                    if cur == sha:
                        con.execute("INSERT OR REPLACE INTO exports(path, sha1, bytes, at) VALUES (?,?,?,?)",
                                    (rel, sha, len(data), now))
                        st["adopted_same"] += 1
                        continue
                    conflicts.append(rel)
                    st["conflict"] += 1
                    continue
                if cur == sha:
                    st["unchanged"] += 1
                    continue
                st["updated"] += 1
            else:
                st["recreated" if rel in manifest else "created"] += 1
            os.makedirs(os.path.dirname(ap), exist_ok=True)
            tmp = ap + ".pwiki-tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, ap)
            con.execute("INSERT OR REPLACE INTO exports(path, sha1, bytes, at) VALUES (?,?,?,?)", (rel, sha, len(data), now))
        for rel, last in sorted(manifest.items()):
            if rel in pages:
                continue
            ap = os.path.join(vault, rel)
            if not os.path.exists(ap):
                con.execute("DELETE FROM exports WHERE path=?", (rel,))
                continue
            with open(ap, "rb") as fh:
                cur = hashlib.sha1(fh.read()).hexdigest()
            if cur != last:
                conflicts.append(rel)
                st["conflict_stale"] += 1
                continue
            os.remove(ap)
            con.execute("DELETE FROM exports WHERE path=?", (rel,))
            st["removed_stale"] += 1
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return {"vault": vault, "pages": len(pages), "counts": dict(st), "conflicts": conflicts[:50],
            "n_conflicts": len(conflicts), "blocked": len(blocked)}
