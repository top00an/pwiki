"""Checks: redact-check (only the count of remaining secrets), verify (raw line-count reconciliation, history reconciliation)."""
import collections
import glob
import json
import os
import re

from . import paths
from .redact import Checker, load_known_values


BINARY_FTS = ("fts_data", "fts_idx")


# JSON 으로 직렬화해 저장한 열. 가림은 JSON 을 푼 문자열에 하므로 검사도 풀어서 문자열마다 한다
# (직렬화된 글에서는 한 필드 끝과 다음 필드 따옴표가 이어져 '따옴표 안 값'처럼 보이는 가짜 일치가 생긴다. 실측 9건 전부).
JSON_COLUMNS = {("events", "raw"), ("tool_uses", "inp"), ("cards", "tools"), ("cards", "files"), ("cards", "cmds"),
                ("cards", "err_sigs"), ("cards", "features"), ("cards", "todos"), ("docs", "meta"), ("runs", "summary")}


def _json_strings(v):
    try:
        o = json.loads(v)
    except ValueError:
        return [v]
    out = []

    def walk(x):
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, list):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            for k, y in x.items():
                out.append(k)
                walk(y)
    walk(o)
    return out


def _scan_tables(con, chk, shape):
    """DB 의 모든 표(FTS 그림자 표 포함)의 문자열·BLOB 값을 논리적으로 훑는다. 구현 규칙(chk)과 독립 형태 규칙(shape)을
    나란히 적용한다. JSON 열은 풀어서 문자열마다 센다. FTS 이진 색인(fts_data·fts_idx)은 트라이그램 조각과 varint 라
    알려진 값만 센다."""
    out = {}
    names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in names:
        if t == "fts":
            continue
        cols = [r[1] for r in con.execute('PRAGMA table_info("%s")' % t.replace('"', '""'))]
        if not cols:
            continue
        ko = t in BINARY_FTS
        cnt = {}
        q = 'SELECT %s FROM "%s"' % (", ".join('"%s"' % c.replace('"', '""') for c in cols), t.replace('"', '""'))
        jcols = [i for i, c in enumerate(cols) if (t, c) in JSON_COLUMNS]
        for row in con.execute(q):
            for i, v in enumerate(row):
                if isinstance(v, str):
                    for x in (_json_strings(v) if i in jcols else (v,)):
                        chk.count_text(x, cnt, known_only=ko)
                        if not ko and shape is not None:
                            shape.text(x)
                elif isinstance(v, (bytes, bytearray)):
                    chk.count_bytes(bytes(v), cnt, known_only=ko)
        out[t] = cnt
    return out


def redact_check(con=None, vault=None, include_logs=True, independent=True):
    """판정 규칙(모두 0 이어야 통과). 값은 출력하지 않고 개수와 모양(마스크)만 보인다.
    구현 규칙(가림과 같은 정규식 + 알려진 값 전체·조각)
    - 알려진 값·조각: DB 파일 바이트 전체·WAL·SHM·표 논리 검사·FTS 색인 질의·vault md·로그.
      조각은 가림보다 엄격하게 센다(길이 max(8, ceil(0.6×길이)) 이상, 숫자·기호 포함).
    - 정규식 형태: 표 논리 검사(FTS 그림자 표 본문 포함)·빈 페이지 바이트·WAL·vault md·로그.
      DB 파일 바이트 전체의 정규식 형태 수는 참고값(살아 있는 페이지의 레코드 머리·넘침 페이지 번호·FTS 이진 색인 바이트가
      글자 사이에 끼어 생기는 조각까지 센다. 살아 있는 값은 표 논리 검사가 본다).
    독립 규칙(가림 정규식을 쓰지 않는 leakscan, 인코딩 변형은 글을 풀어 대조)
    - 원천 문맥 값 대조: 원천에서 비밀번호·토큰 문맥으로 뽑은 값, 수확값, 알려진 값(secrets.local)이
      DB 파일 바이트·WAL·vault md·로그에 나온 수. 알려진 값은 모양을 출력하지 않는다.
    - 문맥 리터럴 형태: 표 논리 검사·vault md·로그에서 문맥 규칙 자리에 가림 토큰이 아닌 값이 남은 수(약한 값 포함).
    - FTS5 secure-delete 설정(지운 행의 색인 조각이 남지 않게)."""
    from .redact import Harvested, freelist_pages, windows
    from . import leakscan
    known = load_known_values()
    harv = Harvested().load()
    chk = Checker(known)
    res = collections.OrderedDict()
    res["known_values"] = len(known)
    res["harvested"] = {"strong": len(harv.active_strong()), "weak_hashes": len(harv.weak),
                        "not_secret_shape": len(harv.strong) - len(harv.active_strong())}
    dbp = paths.db_path()
    if con is not None:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    by = collections.OrderedDict()
    c = {}
    pos = []
    size = chk.count_bytes_file(dbp, c, positions=pos)
    known_db = {k: c.pop(k) for k in ("known", "known_part") if k in c}
    free, ps = freelist_pages(dbp)
    by["db_file_bytes_known"] = {"bytes": size, "hits": known_db, "judged": True}
    by["db_file_bytes_regex_ref"] = {"bytes": size, "hits": c, "judged": False,
                                     "pages": _attribute(con, pos, set(free), ps or 4096)}
    fc = {}
    if free:
        with open(dbp, "rb") as fh:
            for pg in free:
                fh.seek((pg - 1) * ps)
                chk.count_bytes(fh.read(ps), fc)
    by["db_free_pages"] = {"pages": len(free), "hits": fc, "judged": True}
    for label, p in (("wal_bytes", dbp + "-wal"), ("shm_bytes", dbp + "-shm")):
        cc = {}
        by[label] = {"bytes": chk.count_bytes_file(p, cc), "hits": cc, "judged": True}
    shape_db = leakscan.ShapeCounter() if independent else None
    if con is not None:
        tabs = _scan_tables(con, chk, shape_db)
        shadow = {t: v for t, v in tabs.items() if t.startswith("fts_")}
        normal = {t: v for t, v in tabs.items() if not t.startswith("fts_")}
        by["tables_logical"] = {"tables": len(normal), "hits": _sum(normal.values()), "judged": True,
                                "by_table": {t: v for t, v in normal.items() if v}}
        by["fts_shadow_logical"] = {"tables": sorted(shadow), "hits": _sum(shadow.values()), "judged": True,
                                    "by_table": {t: v for t, v in shadow.items() if v}}
        fq = collections.Counter()
        probes = [("known", v) for v in known] + [("known_part", w) for v in known for w in windows(v, frac=0.6)]
        for k, v in probes:
            try:
                fq[k] += con.execute("SELECT count(*) FROM fts WHERE fts MATCH ?", ('"%s"' % v.replace('"', '""'),)).fetchone()[0]
            except Exception:
                pass
        by["fts_index_query_known"] = {"queries": len(probes), "hits": {k: n for k, n in fq.items() if n}, "judged": True}
        r = con.execute("SELECT v FROM fts_config WHERE k='secure-delete'").fetchone()
        by["fts_secure_delete"] = {"setting": r[0] if r else None,
                                   "hits": {} if r and str(r[0]) == "1" else {"secure_delete_off": 1}, "judged": True}
    vault = vault or paths.vault_dir()
    vc = {}
    nfiles = 0
    shape_out = leakscan.ShapeCounter() if independent else None
    if os.path.isdir(vault):
        for dp, dn, fn in os.walk(vault):
            for f in fn:
                if f.endswith(".md"):
                    nfiles += 1
                    fp = os.path.join(dp, f)
                    chk.count_bytes_file(fp, vc)
                    if shape_out is not None:
                        with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                            shape_out.text(fh.read())
    by["vault_md"] = {"files": nfiles, "hits": vc, "judged": True}
    if include_logs:
        lc = {}
        nl = 0
        for d in (paths.logs_dir(), paths.runs_dir()):
            for f in glob.glob(os.path.join(d, "*")):
                if os.path.isfile(f):
                    nl += 1
                    chk.count_bytes_file(f, lc)
                    if shape_out is not None:
                        with open(f, "r", encoding="utf-8", errors="replace") as fh:
                            shape_out.text(fh.read())
        by["logs_runs"] = {"files": nl, "hits": lc, "judged": True}
    if independent:
        by["ind_shape_db"] = {"tables": "표 논리 검사", "hits": dict(shape_db.c) if shape_db else {}, "judged": True,
                              "masks": dict(shape_db.masks.most_common(30)) if shape_db else {}}
        by["ind_shape_files"] = {"files": nfiles, "hits": dict(shape_out.c), "judged": True,
                                 "masks": dict(shape_out.masks.most_common(30))}
        ls = leakscan.scan(db_path=dbp, vault=vault, harvested=harv.active_strong(), known=known)
        by["ind_source_values"] = {"values": ls["values"], "hits": ls["hits"], "judged": True,
                                   "detail": {"values_by_rule": ls["values_by_rule"], "source": ls["source"],
                                              "values_hit": ls["values_hit"], "files": ls["files"]},
                                   "masks": ls["shapes_hit"]}
    res["locations"] = by
    res["total_hits"] = sum(sum(v["hits"].values()) for v in by.values() if v["judged"])
    res["reference_hits"] = sum(sum(v["hits"].values()) for v in by.values() if not v["judged"])
    return res


def _attribute(con, positions, free, ps):
    """바이트 위치 일치를 페이지로 귀속한다(dbstat). 빈 페이지면 'freelist'."""
    out = collections.Counter()
    for k, off in positions:
        if k in ("known", "known_part"):
            continue
        pg = off // ps + 1
        if pg in free:
            out["freelist"] += 1
            continue
        name = None
        if con is not None:
            try:
                r = con.execute("SELECT name, pagetype FROM dbstat WHERE pageno=?", (pg,)).fetchone()
                if r:
                    name = "%s/%s" % (r[0], r[1])
            except Exception:
                name = None
        out[name or "알 수 없음"] += 1
    return dict(out)


def _sum(dicts):
    out = {}
    for d in dicts:
        for k, v in d.items():
            out[k] = out.get(k, 0) + v
    return out


LABELS = {
    "db_file_bytes_known": "[구현] DB 파일 바이트 전체: 알려진 값·조각",
    "db_file_bytes_regex_ref": "[구현] DB 파일 바이트 전체: 정규식 형태(참고)",
    "db_free_pages": "[구현] DB 빈 페이지(freelist) 바이트",
    "wal_bytes": "[구현] WAL 바이트",
    "shm_bytes": "[구현] SHM 바이트",
    "tables_logical": "[구현] 표 논리 검사(일반 표)",
    "fts_shadow_logical": "[구현] FTS 그림자 표 논리 검사",
    "fts_index_query_known": "[구현] FTS 색인 질의(알려진 값·조각 MATCH)",
    "fts_secure_delete": "FTS5 secure-delete 설정",
    "vault_md": "[구현] vault md",
    "logs_runs": "[구현] 로그·실행 기록",
    "ind_shape_db": "[독립] 문맥 리터럴 형태: 표 논리 검사",
    "ind_shape_files": "[독립] 문맥 리터럴 형태: vault md·로그",
    "ind_source_values": "[독립] 원천 문맥 값·수확값·알려진 값 대조(풀어서 대조): DB 바이트·WAL·vault·로그",
}


def format_redact_check(res):
    from .redact import PATTERNS, EMAIL_KEEP_LOCALPARTS
    out = ["# redact-check",
           "알려진 값 %d개 · 수확값(강한 값) %d개(비밀 아닌 모양이라 뺀 값 %d개) · 약한 수확값 해시 %d개 · 구현 정규식 %d종 ·"
           " 값은 출력하지 않고 개수와 모양만 센다" % (
               res["known_values"], res["harvested"]["strong"], res["harvested"].get("not_secret_shape", 0),
               res["harvested"]["weak_hashes"], len(PATTERNS)),
           "이메일 예외(서버 주소로 남김): 로컬부가 %s 인 주소" % "·".join(EMAIL_KEEP_LOCALPARTS), ""]
    out.append("| 위치 | 대상 | 판정 대상 | 남은 개수 |")
    out.append("|---|---|---|---|")
    for k, v in res["locations"].items():
        if v.get("bytes") is not None:
            tgt = "%s바이트" % "{:,}".format(v["bytes"] or 0)
        elif "pages" in v:
            tgt = "페이지 %d" % v["pages"]
        elif "files" in v:
            tgt = "파일 %d" % v["files"]
        elif "queries" in v:
            tgt = "질의 %d" % v["queries"]
        elif "values" in v:
            tgt = "값 %d개" % v["values"]
        elif "setting" in v:
            tgt = "설정값 %s" % v["setting"]
        elif isinstance(v.get("tables"), str):
            tgt = v["tables"]
        else:
            tgt = ("표 %d" % v["tables"]) if isinstance(v["tables"], int) else ("표 %d개(%s)" % (len(v["tables"]), ", ".join(v["tables"])))
        hits = v["hits"]
        out.append("| %s | %s | %s | %d%s |" % (LABELS.get(k, k), tgt, "예" if v["judged"] else "아니오(참고)", sum(hits.values()),
                                              (" (" + ", ".join("%s %d" % kv for kv in sorted(hits.items())) + ")") if hits else ""))
    ref = res["locations"].get("db_file_bytes_regex_ref", {}).get("pages")
    if ref:
        out.append("")
        out.append("참고값이 있는 페이지(표/페이지 종류): " + ", ".join("%s %d" % kv for kv in sorted(ref.items())))
    isv = res["locations"].get("ind_source_values")
    if isv:
        d = isv["detail"]
        out.append("")
        out.append("독립 대조 원천: 줄 %s(사전검사 통과 %s) · 문서 %s · 뽑은 값 %d개(규칙별 %s) · 대조 파일 %d · 남은 값 %d개" % (
            "{:,}".format(d["source"].get("lines", 0)), "{:,}".format(d["source"].get("trigger_lines", 0)),
            "{:,}".format(d["source"].get("docs", 0)), isv["values"],
            ", ".join("%s %d" % kv for kv in sorted(d["values_by_rule"].items())), d["files"], d["values_hit"]))
    for k in ("ind_shape_db", "ind_shape_files", "ind_source_values"):
        v = res["locations"].get(k)
        if v and v.get("masks"):
            out.append("- %s 남은 모양(규칙 마스크 개수): %s" % (LABELS[k], ", ".join("%s %d" % kv for kv in v["masks"].items())))
    out.append("")
    out.append("판정 대상 합계 %d건 → %s · 참고값 %d건" % (res["total_hits"], "통과(0건)" if res["total_hits"] == 0 else "실패",
                                                  res["reference_hits"]))
    out.append("참고값: 살아 있는 페이지의 레코드 머리·넘침 페이지 번호·FTS 이진 색인 바이트가 글자 사이에 끼어 생긴 조각까지 센 수다."
               " 살아 있는 값은 표 논리 검사가, 지운 내용은 빈 페이지 검사가 판정한다.")
    return "\n".join(out)


def verify(con):
    """파일마다 원문을 저장된 오프셋까지 다시 세어 '원문 uuid 줄 = 적재 + 사유별 제외' 를 독립 확인한다."""
    claude = paths.claude_dir()
    stored_u = dict(con.execute("SELECT fid, count(*) FROM events WHERE uuid IS NOT NULL GROUP BY fid"))
    stored_a = dict(con.execute("SELECT fid, count(*) FROM events GROUP BY fid"))
    ex_u = dict(con.execute("SELECT fid, sum(n) FROM excl WHERE has_uuid=1 GROUP BY fid"))
    ex_a = dict(con.execute("SELECT fid, sum(n) FROM excl GROUP BY fid"))
    tot = collections.Counter()
    bad = []
    missing = 0
    for path, fid, off, kind in con.execute("SELECT path, fid, off, kind FROM files WHERE kind IN "
                                            "('session','subagent','wf_agent','journal','history')"):
        ap = paths.source_path(path, claude)
        if not os.path.exists(ap):
            missing += 1
            continue
        lines = uu = 0
        with open(ap, "rb") as fh:
            data = fh.read(off)
        parts = data.split(b"\n")
        if parts and parts[-1] == b"":
            parts.pop()
        for raw in parts:
            lines += 1
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            if isinstance(o, dict) and isinstance(o.get("uuid"), str) and o.get("uuid"):
                uu += 1
        su, sa, eu, ea = stored_u.get(fid, 0), stored_a.get(fid, 0), ex_u.get(fid, 0) or 0, ex_a.get(fid, 0) or 0
        tot["files"] += 1
        tot["raw_lines"] += lines
        tot["raw_uuid"] += uu
        tot["stored_all"] += sa
        tot["stored_uuid"] += su
        tot["excluded_all"] += ea
        tot["excluded_uuid"] += eu
        if uu != su + eu or lines != sa + ea:
            bad.append((path, uu, su, eu, lines, sa, ea))
    reasons = collections.Counter()
    for r, hu, n in con.execute("SELECT reason, has_uuid, sum(n) FROM excl GROUP BY reason, has_uuid"):
        reasons[(r, hu)] = n
    hist = history_crosscheck(con)
    return {"totals": dict(tot), "mismatch": bad, "missing_files": missing,
            "excluded_by_reason": sorted(((r, hu, n) for (r, hu), n in reasons.items()), key=lambda x: -x[2]),
            "history": hist, "formats": format_distribution(con)}


def format_distribution(con):
    """형식 변화 감시: version 별 줄 수·기간·type 분포와 처음 본 표본 키, 모르는 형식으로 적재한 줄."""
    by_ver = con.execute("SELECT COALESCE(ver, '(없음)'), count(*), min(ts), max(ts) FROM events GROUP BY 1 ORDER BY 3").fetchall()
    types = collections.defaultdict(collections.Counter)
    for ver, typ, n in con.execute("SELECT COALESCE(ver, '(없음)'), type, count(*) FROM events GROUP BY 1, 2"):
        types[ver][typ] = n
    # type·sub 조합이 처음 나온 판과 표본 키(가장 이른 줄)
    # 집계가 min() 하나뿐이면 SQLite 는 나머지 열을 최소값 행에서 가져온다(가장 이른 줄의 판·키).
    firsts = [(t, s, v, k, ts) for t, s, ts, v, k in con.execute(
        "SELECT type, COALESCE(sub, ''), min(ts), COALESCE(ver, '(없음)'), key FROM events"
        " WHERE ts IS NOT NULL GROUP BY type, COALESCE(sub, '') ORDER BY 3")]
    unknown = con.execute("SELECT sub, count(*), min(key), min(ts), max(ts) FROM events WHERE kind='unknown' GROUP BY sub"
                          " ORDER BY 2 DESC").fetchall()
    return {"by_version": by_ver, "types": {k: dict(v) for k, v in types.items()}, "firsts": firsts, "unknown": unknown}


_PH_RX = re.compile(r"\[(?:Pasted text #\d+(?: \+\d+ lines)?|Image #\d+)\]|\[REDACTED:[a-z_]+\]")
# 카드 사람 입력에서 지울 감싸개: 붙여넣은 글 감싸개(<pasted_content id=…>)와 가림 토큰(history 와 카드가 다른 실행에서 가려져
# 토큰 자리가 어긋날 수 있다). 지운 자리는 공백으로 둔다.
_WRAP_RX = re.compile(r"</?pasted_content(?: id=\"[^\"]*\")?>")
_GOAL_CONTROL = {"stop", "clear", "pause", "resume", "off", "cancel", "status", "done", "reset"}
# 사람 입력 원문이 남는 본선 줄의 kind(답·도구 결과·압축 요약·작업 알림·명령 출력·곁가지(하위 에이전트 지시)는 사람이 친 글이나
# 경로를 옮겨 적을 수 있어 뺀다). att 는 프롬프트 모양 queued_command 와 goal_status, system 은 local_command 만 본다.
_INPUT_KINDS = ("human", "sdk", "queued", "qo_human", "bash_in", "slash", "command", "user_other", "meta", "interrupt",
                "peer")


def _norm(s):
    return " ".join((s or "").split())


def _card_norm(s):
    return _norm(_PH_RX.sub(" ", _WRAP_RX.sub(" ", s or "")))


_CMD_NAME_RX = re.compile(r"<command-name>\s*(/?[^<\s]*)\s*</command-name>")
COUNTED_SLASH_KINDS = ("command", "slash", "human")


def _slash_lines(con):
    """세션 기록(최상위 세션 파일)의 슬래시 명령 줄을 세션·이름별로 센다.
    slash_counted: 사람 입력으로 세는 user 줄(kind command·slash·human) / slash_uncounted: 그 밖의 user 줄(곁가지 제외)
    / slash_local_command: system/local_command 로 적힌 줄(참고)."""
    out = collections.defaultdict(lambda: collections.defaultdict(collections.Counter))
    q = ("SELECT sid, type, kind, sub, side, text FROM events WHERE (ltrim(text) LIKE '/%' OR text LIKE '<command-name>%')"
         " AND fid IN (SELECT fid FROM files WHERE kind='session')")
    for sid, typ, kind, sub, side, text in con.execute(q):
        t = (text or "").strip()
        m = _CMD_NAME_RX.match(t)
        if m:
            name = m.group(1)
            name = name if name.startswith("/") else "/" + name
        else:
            name = t.split()[0] if t.startswith("/") else None
        if not name:
            continue
        if typ == "user" and not side:
            out[sid]["slash_counted" if kind in COUNTED_SLASH_KINDS else "slash_uncounted"][name] += 1
        elif typ == "system" and sub == "local_command":
            out[sid]["slash_local_command"][name] += 1
    return out


def history_crosscheck(con, cards_human=None):
    """history.jsonl 사람 프롬프트와 수집된 사람 입력(카드)을 세션별로 대조한다.
    history 는 붙여넣은 글을 [Pasted text #N] 자리표시로 적으므로 자리표시·가림 토큰으로 나눈 첫 조각(4자 이상)의 앞 30자가
    카드 사람 입력(붙여넣기 감싸개·가림 토큰을 지운 글)에 들어 있으면 대조된 것으로 본다. '! 명령'은 명령 부분, '/명령 인자'는
    인자 부분으로 대조한다. 자리표시만 있으면 pastedContents 의 글 앞 30자로 대조한다.
    /goal 제어(stop·clear 등)는 따로 센다. 대조 안 된 것은 사유로 나눈다(_unmatched_reason): missed 가 진짜 누락이다."""
    have = collections.defaultdict(list)
    # cards_human: (sid, 사람 입력) 목록을 주면 cards 표 대신 그것과 대조한다(미리보기: 다시 계산한 카드로 대조).
    for sid, human in (cards_human if cards_human is not None else con.execute("SELECT sid, human FROM cards")):
        have[sid].append(_card_norm(human))
    sess_files = {r[0] for r in con.execute("SELECT sid FROM files WHERE kind='session'")}
    slash_lines = _slash_lines(con)
    out = collections.Counter()
    by_proj = collections.defaultdict(collections.Counter)
    examples = []
    missed = []
    for sid, text, raw, proj in con.execute("SELECT sid, text, raw, project FROM events WHERE kind='history'"):
        if sid not in sess_files:
            out["no_session_record"] += 1
            by_proj[proj]["no_session_record"] += 1
            continue
        disp = _norm(text)
        if disp.startswith("/") and " " not in disp:
            # 인자 없는 슬래시: 같은 세션의 줄과 이름으로 짝짓는다(같은 이름이 여러 번이면 개수만큼).
            # slash_uncounted 는 user 줄이 있는데 사람 입력으로 세지 않은 것(누락 감시, 0 이어야 한다).
            have_s = slash_lines.get(sid, {})
            for bucket in ("slash_counted", "slash_uncounted", "slash_local_command"):
                if have_s.get(bucket, collections.Counter())[disp] > 0:
                    have_s[bucket][disp] -= 1
                    break
            else:
                bucket = "slash_no_line"
            out[bucket] += 1
            by_proj[proj][bucket] += 1
            if bucket == "slash_uncounted":
                missed.append((sid[:8], "[user 줄 있으나 사람 입력으로 안 셈] " + disp[:40]))
            continue
        dp = disp.split()
        if len(dp) == 2 and dp[0] == "/goal" and dp[1].lower() in _GOAL_CONTROL:
            # /goal 제어(stop·clear 등)는 작업 입력이 아니다. 낱말이 짧아 다른 글과 우연히 대조되므로 따로 센다.
            out["goal_control"] += 1
            by_proj[proj]["goal_control"] += 1
            continue
        body = disp
        if body.startswith("!"):
            body = body[1:].lstrip()
        elif body.startswith("/") and " " in body:
            body = body.split(" ", 1)[1]
        segs = [_norm(x) for x in _PH_RX.split(body)]
        segs = [x for x in segs if len(x) >= 4]
        probe = segs[0][:30] if segs else ""
        if len(probe) < 4:
            try:
                pc = json.loads(raw or "{}").get("pastedContents") or {}
                if isinstance(pc, str):
                    pc = json.loads(pc or "{}")
                vals = [v.get("content") for v in pc.values() if isinstance(v, dict) and isinstance(v.get("content"), str)]
            except (ValueError, AttributeError):
                vals = []
            probe = _norm(vals[0])[:30] if vals else ""
        if len(probe) < 4:
            out["placeholder_only"] += 1
            by_proj[proj]["placeholder_only"] += 1
            continue
        if any(probe in h for h in have.get(sid, [])):
            out["matched"] += 1
            by_proj[proj]["matched"] += 1
            continue
        # 흔적 찾기는 첫 조각 전체로 한다: 앞 30자·120자 조각은 흔한 경로 앞부분(스크래치패드 경로 등)과 우연히 맞았다(실측).
        reason = _unmatched_reason(con, sid, disp, (segs[0] if segs and len(segs[0]) > len(probe) else probe))
        out["unmatched"] += 1
        out["unmatched_" + reason] += 1
        by_proj[proj]["unmatched"] += 1
        by_proj[proj]["unmatched_" + reason] += 1
        if reason == "missed":
            missed.append((sid[:8], "[누락: 세션 기록에 입력 줄이 있는데 카드에 없음] " + disp[:40]))
        elif len(examples) < 8:
            examples.append((sid[:8], "[%s] %s" % (reason, disp[:40])))
    return {"totals": dict(out), "by_project": {k: dict(v) for k, v in by_proj.items()},
            "unmatched_examples": missed[:20] + examples, "missed": len(missed)}


def _unmatched_reason(con, sid, disp, probe):
    """대조 안 된 history 입력의 사유. 같은 세션의 입력 성격 줄(사람·첨부·대기열·system) 글에 대조 조각이 있으면
    'missed'(입력은 기록됐는데 사람 입력으로 세지 않음, 0 이어야 한다). 없으면 세션 기록에 흔적이 없는 입력이다:
    'btw'(/btw 곁가지 질문), 'history_only'(그 밖). /goal 제어는 대조 전에 goal_control 로 따로 센다."""
    parts = disp.split()
    q = ("SELECT text FROM events WHERE sid=? AND (kind IN (%s) OR (kind='att' AND sub='goal_status')"
         " OR (kind='att' AND sub='queued_command' AND json_extract(raw, '$.attachment.commandMode')='prompt')"
         " OR (kind='system' AND sub='local_command')) AND fid IN (SELECT fid FROM files WHERE kind='session')"
         " AND text IS NOT NULL" % ",".join("?" * len(_INPUT_KINDS)))
    for (t,) in con.execute(q, (sid,) + _INPUT_KINDS):
        if probe in _card_norm(t):
            return "missed"
    if parts and parts[0] == "/btw":
        return "btw"
    return "history_only"
