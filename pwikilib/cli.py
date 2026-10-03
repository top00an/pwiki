"""명령줄. 사용: pwiki <명령> [옵션]"""
import argparse
import json
import sys

from . import VERSION
from . import db as dbm
from . import paths


def _p(s):
    sys.stdout.write(s + ("\n" if not s.endswith("\n") else ""))


def cmd_ingest(a):
    from . import ingest
    try:
        s = ingest.run(project=a.project)
    except ingest.Busy as e:
        _p("중단: %s" % e)
        return 75
    _p(format_ingest(s))
    return 0 if s["accounting"]["mismatch_files"] == 0 else 2


def format_ingest(s):
    c = s["counts"]
    acc = s["accounting"]
    t = acc["totals"]
    out = ["# ingest", "원천 파일 종류별: " + ", ".join("%s %d" % kv for kv in sorted(s["kinds"].items())),
           "읽은 줄 %s · 새 행 %s · 파싱 실패 %s · 처음부터 다시 읽은 파일 %s · 바뀐 파일 %s · 문서 새로 %s · 문서 바뀜 %s" % tuple(
               "{:,}".format(c.get(k, 0)) for k in ("lines_read", "rows_new", "bad_json", "files_reset", "files_changed",
                                                     "docs_new", "docs_changed")),
           "카드: 다시 만든 세션 %d · 카드 %d" % (c.get("sessions_rebuilt", 0), c.get("cards_built", 0)),
           "사라진 원천(보존 중): 줄 파일 %d · 문서 %d" % (c.get("files_missing_now", 0), c.get("docs_missing_now", 0)),
           "소요 %.1f초 · 알려진 비밀값 %d개" % (s["elapsed_s"], s["known_values"])]
    ch = s.get("changed_files") or []
    if ch:
        out.append("")
        out.append("## 이번 실행에서 새 줄을 읽은 파일 %d개(상위 10, 읽은 줄 · 새 행 · 방식)" % len(ch))
        for rel, nl, ns, how in sorted(ch, key=lambda x: -x[1])[:10]:
            out.append("- %s · %s줄 · %s행 · %s" % (rel, "{:,}".format(nl), "{:,}".format(ns), how))
    if s["excluded"]:
        out.append("")
        out.append("## 이번 실행에서 제외한 줄(사유별)")
        for k, v in sorted(s["excluded"].items(), key=lambda kv: -kv[1]):
            out.append("- %s: %s" % (k, "{:,}".format(v)))
    if s["strips"]:
        out.append("")
        out.append("## 이번 실행에서 자리표시로 바꾼 필드(사유별 개수·바이트)")
        for k, (n, b) in sorted(s["strips"].items(), key=lambda kv: -kv[1][1]):
            out.append("- %s: %s건 · %.1fMB" % (k, "{:,}".format(n), b / 1e6))
    if s.get("unknown_kept"):
        out.append("")
        out.append("## 모르는 type·첨부(가린 뒤 kind=unknown 으로 적재)")
        out.append(", ".join("%s %s" % (k, "{:,}".format(v)) for k, v in sorted(s["unknown_kept"].items())))
    hv = s.get("harvest") or {}
    if hv:
        out.append("")
        out.append("## 수확(가림 전, 비밀번호·토큰 문맥의 값. 값은 출력하지 않음)")
        out.append("훑은 줄 %s · 사전검사 통과 %s · 후보 %s · 새 강한 값 %s(누계 %s) · 새 약한 값 해시 %s(누계 %s) · %s초" % tuple(
            "{:,}".format(hv.get(k, 0)) if k != "seconds" else str(hv.get(k, 0)) for k in (
                "harvest_lines", "harvest_trigger_lines", "candidates", "strong_new", "strong_total", "weak_new",
                "weak_total", "seconds")))
    sh = s.get("stored_harvest")
    if sh:
        out.append("")
        out.append("## 저장 행 수확(규칙 판이 바뀐 뒤 한 번. 지금 규칙이 새로 잡는 문맥의 값. 값은 출력하지 않음)")
        srt = sh.get("retro") or {}
        out.append("훑은 events %s(사전검사 통과 %s) · docs %s · 후보 %s · 새 강한 값 %s · 새 약한 값 해시 %s · 소급 대상 값 %s ·"
                   " 소급으로 바뀐 events %s · docs %s · %s초" % (
                       "{:,}".format(sh.get("rows", 0)), "{:,}".format(sh.get("trigger_rows", 0)),
                       "{:,}".format(sh.get("docs", 0)), sh.get("candidates", 0), sh.get("strong_new", 0),
                       sh.get("weak_new", 0), sh.get("retro_values", 0), srt.get("events_changed", 0),
                       srt.get("docs_changed", 0), sh.get("seconds", 0)))
    rt = s.get("retro")
    if rt:
        out.append("")
        out.append("## 소급 가림(새 수확값·바뀐 secrets.local 값이 든 저장 행만 다시 가림. 값은 출력하지 않음)")
        out.append("대상 값 %s · 약한 값 해시 %s · 훑은 events %s · 닻 통과 %s · 바뀐 events %s · 바뀐 docs %s · 카드 다시 만든 세션 %s · %s초" % (
            tuple("{:,}".format(rt.get(k, 0)) for k in ("values", "weak_hashes", "events", "events_checked",
                                                      "events_changed", "docs_changed", "sessions_rebuilt"))
            + (rt.get("seconds", 0),)))
    if s["redactions"]:
        out.append("")
        out.append("## 이번 실행의 가림 횟수(종류별)")
        out.append(", ".join("%s %s" % (k, "{:,}".format(v)) for k, v in sorted(s["redactions"].items(), key=lambda kv: -kv[1])))
    if s.get("unknown_fields"):
        out.append("")
        out.append("## 모르는 필드(raw 에는 남기고 해석하지 않음, 상위 15)")
        uf = sorted(s["unknown_fields"].items(), key=lambda kv: -kv[1])
        out.append(", ".join("%s %s" % (k, "{:,}".format(v)) for k, v in uf[:15]) + (" 외 %d종" % (len(uf) - 15) if len(uf) > 15 else ""))
    if s["unrecognized"]:
        out.append("")
        out.append("## 원천으로 보지 않은 파일(확장자별)")
        out.append(", ".join("%s %d" % kv for kv in sorted(s["unrecognized"].items())))
    out.append("")
    out.append("## 줄 수 대조(파일마다: 원문 uuid 줄 = 적재 uuid 행 + 제외 uuid 줄, 전체 줄 = 적재 행 + 제외 줄)")
    out.append("파일 %d · 불일치 파일 %d · 원문 uuid 줄 %s = 적재 %s + 제외 %s · 전체 줄 %s = 적재 %s + 제외 %s" % (
        acc["files"], acc["mismatch_files"], *("{:,}".format(t.get(k, 0)) for k in (
            "uuid_lines", "stored_uuid", "excluded_uuid", "lines", "stored_all", "excluded_all"))))
    for m in acc["mismatch"]:
        out.append("- 불일치 %s: %s" % (m["path"], json.dumps({k: v for k, v in m.items() if k != "path"})))
    out.append("")
    tt = s["totals"]
    out.append("DB 누계: " + ", ".join("%s %s" % (k, "{:,}".format(v)) for k, v in tt.items()))
    if s["warnings"]:
        out.append("경고: " + "; ".join(s["warnings"]))
    return "\n".join(out)


def _con(readonly=False):
    import os
    if not os.path.exists(paths.db_path()):
        _p("DB 가 없다: 먼저 pwiki ingest 를 돌린다 (%s)" % paths.db_path())
        sys.exit(3)
    return dbm.open_db(readonly=readonly)


def cmd_rederive(a):
    import os
    from . import rederive
    if not os.path.exists(paths.db_path()):
        _p("DB 가 없다: %s" % paths.db_path())
        return 3
    if a.apply:
        from . import ingest
        try:
            s = rederive.run(apply_changes=True)
        except ingest.Busy as e:
            _p("중단: %s" % e)
            return 75
    else:
        s = rederive.run(apply_changes=False)
    _p(rederive.format_run(s))
    return 0


def cmd_redact_check(a):
    from . import check
    con = _con()
    res = check.redact_check(con)
    _p(check.format_redact_check(res))
    return 0 if res["total_hits"] == 0 else 1


def cmd_verify(a):
    from . import check
    con = _con()
    r = check.verify(con)
    t = r["totals"]
    out = ["# verify(원문을 저장된 오프셋까지 다시 세어 대조)",
           "파일 %d · 원문 줄 %s · 원문 uuid 줄 %s" % (t.get("files", 0), "{:,}".format(t.get("raw_lines", 0)),
                                               "{:,}".format(t.get("raw_uuid", 0))),
           "적재 행 %s(uuid %s) · 제외 줄 %s(uuid %s)" % tuple("{:,}".format(t.get(k, 0)) for k in (
               "stored_all", "stored_uuid", "excluded_all", "excluded_uuid")),
           "원문 uuid 줄 = 적재 uuid + 제외 uuid: %s · 원문 줄 = 적재 + 제외: %s" % (
               "성립" if t.get("raw_uuid", 0) == t.get("stored_uuid", 0) + t.get("excluded_uuid", 0) else "불성립",
               "성립" if t.get("raw_lines", 0) == t.get("stored_all", 0) + t.get("excluded_all", 0) else "불성립"),
           "불일치 파일 %d · 원천이 사라진 파일 %d(DB 에 보존)" % (len(r["mismatch"]), r["missing_files"]), ""]
    for m in r["mismatch"][:20]:
        out.append("- 불일치 %s: uuid %d = %d + %d ? · 줄 %d = %d + %d ?" % m)
    out.append("## 제외 사유별 줄 수(누계)")
    out.append("| 사유 | uuid 있음 | 줄 수 |")
    out.append("|---|---|---|")
    for rsn, hu, n in r["excluded_by_reason"]:
        out.append("| %s | %s | %s |" % (rsn, "예" if hu else "아니오", "{:,}".format(n)))
    h = r["history"]
    f = r["formats"]
    out.append("")
    out.append("## 형식 분포(형식 변화 감시)")
    out.append("### version 별 적재 줄 수·기간(KST)·type 분포")
    out.append("| version | 줄 | 처음 | 마지막 | type(많은 순 상위 6) |")
    out.append("|---|---|---|---|---|")
    for ver, n, t0, t1 in f["by_version"]:
        ty = sorted(f["types"].get(ver, {}).items(), key=lambda kv: -kv[1])
        out.append("| %s | %s | %s | %s | %s |" % (ver, "{:,}".format(n), paths.kst_str(t0), paths.kst_str(t1),
                                                ", ".join("%s %s" % (k, "{:,}".format(v)) for k, v in ty[:6])
                                                + (" 외 %d종" % (len(ty) - 6) if len(ty) > 6 else "")))
    out.append("### type·sub 조합이 처음 나온 판과 표본 키(%d종)" % len(f["firsts"]))
    groups = {}
    for typ, sub, ver, key, ts in f["firsts"]:
        groups.setdefault(typ, []).append("%s(%s, %s, [%s])" % (sub or "-", ver, paths.kst_str(ts, "%m-%d"), key))
    for typ in sorted(groups, key=lambda t: -len(groups[t])):
        items = groups[typ]
        out.append("- %s %d종: %s%s" % (typ, len(items), "; ".join(items[:6]), " 외 %d종" % (len(items) - 6) if len(items) > 6 else ""))
    out.append("### 모르는 형식으로 적재한 줄(kind=unknown, 가린 뒤 원문 JSON 보존)")
    if f["unknown"]:
        for sub, n, key, t0, t1 in f["unknown"]:
            out.append("- %s: %d줄 · %s ~ %s · 표본 [%s]" % (sub, n, paths.kst_str(t0), paths.kst_str(t1), key))
    else:
        out.append("- 없음")
    out.append("")
    out.append("## history.jsonl 사람 프롬프트 대조(세션별 카드의 사람 입력 앞 40자)")
    out.append(", ".join("%s %d" % kv for kv in sorted(h["totals"].items())))
    out.append("대조 안 됨 사유: missed = 세션 기록에 입력 줄이 있는데 사람 입력으로 세지 않음(누락, 0 이어야 한다) ·"
               " btw = /btw 곁가지 질문 · history_only = 세션 기록에 흔적 없음. goal_control = /goal stop·clear 등 제어(대조 전에 따로 셈)."
               " 인자 없는 슬래시의 slash_uncounted 도 누락으로 센다.")
    out.append("판정: 누락 %d건 → %s" % (h["missed"], "통과(0건)" if h["missed"] == 0 else "실패"))
    for sid, head in h["unmatched_examples"]:
        out.append("- 대조 안 됨 %s: %s" % (sid, head))
    _p("\n".join(out))
    return 0 if not r["mismatch"] and not h["missed"] else 2


def cmd_day(a):
    from . import views
    con = _con(readonly=True)
    _p(views.day(con, a.date))
    return 0


def cmd_today(a):
    from . import views
    con = _con(readonly=True)
    _p(views.day(con, paths.today_kst()))
    return 0


def cmd_eff(a):
    from . import views
    con = _con(readonly=True)
    proj = None
    if a.project:
        proj = views.resolve_project(con, a.project)
        if not proj:
            _p("프로젝트를 찾지 못했다: %s" % a.project)
            return 3
    _p(views.eff(con, proj, a.since))
    return 0


def cmd_resume(a):
    from . import views
    con = _con(readonly=True)
    proj = views.resolve_project(con, a.project)
    if not proj:
        _p("프로젝트를 찾지 못했다: %s" % a.project)
        return 3
    _p(views.resume(con, proj, a.max_chars))
    return 0


def cmd_search(a):
    from . import views
    con = _con(readonly=True)
    proj = None
    if a.project:
        proj = views.resolve_project(con, a.project)
        if not proj:
            _p("프로젝트를 찾지 못했다: %s" % a.project)
            return 3
    res = views.search(con, a.query, proj, a.since, a.kind, a.limit, until=a.until, exclude=a.exclude_session,
                       per_session=a.per_session, include_all=a.all)
    _p(views.format_search(res, a.query))
    return 0 if res["rows"] else 1


def cmd_show(a):
    from . import views
    con = _con(readonly=True)
    ref = " ".join(a.key)
    out = views.show(con, ref)
    if out is None:
        _p("키를 찾지 못했다: %s (search 출력의 키, '카드 <키>', '문서 <경로>', day 의 sid8/uuid8 을 받는다)" % ref)
        return 1
    _p(out)
    return 0


def cmd_export(a):
    from . import export
    con = _con()
    r = export.export(con, a.vault)
    out = ["# export", "vault %s · 페이지 %d · %s" % (r["vault"], r["pages"],
                                                  ", ".join("%s %d" % kv for kv in sorted(r["counts"].items()))),
           "충돌(사람이 고친 파일, 덮지 않음) %d · 가림 검사로 막은 페이지 %d" % (r["n_conflicts"], r["blocked"])]
    for c in r["conflicts"][:20]:
        out.append("- 충돌: %s" % c)
    _p("\n".join(out))
    return 0 if not r["n_conflicts"] and not r["blocked"] else 4


def build_parser():
    ap = argparse.ArgumentParser(prog="pwiki", description="Claude Code 기록 결정론 wiki (1단계, LLM 없음)")
    ap.add_argument("--version", action="version", version="pwiki " + VERSION)
    sp = ap.add_subparsers(dest="cmd")
    p = sp.add_parser("ingest", help="원천을 이어 읽어 적재")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="모든 프로젝트와 history.jsonl(기본)")
    g.add_argument("--project", help="~/.claude/projects 아래 폴더명 하나만")
    p.set_defaults(fn=cmd_ingest)
    p = sp.add_parser("rederive", help="저장된 행에 지금 가림·분류·카드 규칙을 다시 적용(기본은 읽기 전용 미리보기)")
    p.add_argument("--apply", action="store_true", help="실제로 고친다(수집 잠금 아래 한 트랜잭션, 백업 사본 없음)")
    p.set_defaults(fn=cmd_rederive)
    p = sp.add_parser("redact-check", help="DB 바이트·WAL·FTS 그림자 표·vault md·로그에서 남은 비밀 개수")
    p.set_defaults(fn=cmd_redact_check)
    p = sp.add_parser("verify", help="원문 줄 수 독립 대조와 history 대조")
    p.set_defaults(fn=cmd_verify)
    p = sp.add_parser("day", help="그날(KST) 프로젝트별 카드 목록")
    p.add_argument("date", help="YYYY-MM-DD")
    p.set_defaults(fn=cmd_day)
    p = sp.add_parser("today", help="오늘(KST) 카드 목록")
    p.set_defaults(fn=cmd_today)
    p = sp.add_parser("eff", help="효율 지표(숫자만)")
    p.add_argument("--project")
    p.add_argument("--since", help="YYYY-MM-DD(KST)")
    p.set_defaults(fn=cmd_eff)
    p = sp.add_parser("resume", help="이어 하기 상태(마크다운, 상한 안)")
    p.add_argument("--project", required=True, help="폴더명 또는 cwd")
    p.add_argument("--max-chars", type=int, default=4000)
    p.set_defaults(fn=cmd_resume)
    p = sp.add_parser("search", help="FTS5 trigram 검색(3글자 미만 낱말은 LIKE)")
    p.add_argument("query")
    p.add_argument("--project")
    p.add_argument("--since", help="YYYY-MM-DD(KST)")
    p.add_argument("--kind", help="human, assistant, tool_result, card, compact, history, doc(:memory 등) …")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--until", help="YYYY-MM-DD(KST, 그날까지 포함)")
    p.add_argument("--exclude-session", action="append", default=[], help="뺄 세션 id(앞부분도 됨). 여러 번 줄 수 있다")
    p.add_argument("--per-session", type=int, default=3, help="같은 세션 결과를 먼저 싣는 상한(0 이면 끔, 기본 3)")
    p.add_argument("--all", action="store_true", help="하위 에이전트·워크플로 파일 행도 넣는다(기본은 뺀다)")
    p.set_defaults(fn=cmd_search)
    p = sp.add_parser("show", help="키 하나의 전문(DB 에 가려 저장된 글). search·resume·day 출력의 키를 그대로 받는다")
    p.add_argument("key", nargs="+", help="예: <sid>:<uuid> · 카드 <sid>:<uuid> · 문서 <경로> · <sid8>/<uuid8>")
    p.set_defaults(fn=cmd_show)
    p = sp.add_parser("export", help="Obsidian vault 로 md 내보내기")
    p.add_argument("--vault", help="기본 ~/pwiki")
    p.set_defaults(fn=cmd_export)
    return ap


def _fix_dash_values(argv):
    """폴더명이 '-' 로 시작하므로(-Users-...) '--project -Users-…' 를 '--project=-Users-…' 로 붙인다."""
    out = []
    i = 0
    while i < len(argv):
        x = argv[i]
        if x in ("--project",) and i + 1 < len(argv) and argv[i + 1].startswith("-") and argv[i + 1] != "--":
            out.append(x + "=" + argv[i + 1])
            i += 2
            continue
        out.append(x)
        i += 1
    return out


def main(argv):
    import os
    os.umask(0o077)
    ap = build_parser()
    a = ap.parse_args(_fix_dash_values(list(argv)))
    if not getattr(a, "fn", None):
        ap.print_help()
        return 1
    return a.fn(a)
