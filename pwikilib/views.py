"""보기: day·today·eff·resume·search. 모두 DB 를 읽기만 한다. 해석·추천 문장은 쓰지 않는다."""
import collections
import json
import os
import re

from . import parse
from . import paths

HOME_PREFIX = "-" + paths.HOME_USER.strip("/").replace("/", "-")


def short_project(p):
    if not p:
        return "-"
    if p == HOME_PREFIX:
        return "~"
    if p.startswith(HOME_PREFIX + "-"):
        return p[len(HOME_PREFIX) + 1:]
    return p


def fmt_dur(s):
    if s is None:
        return "-"
    s = int(round(s))
    if s < 60:
        return "%d초" % s
    if s < 3600:
        return "%d분" % (s // 60)
    h, m = divmod(s // 60, 60)
    return "%d시간 %d분" % (h, m)


_IMG_PH = re.compile(r"\[이미지 생략 base64 \d+바이트 [^\]]*\]")


def one_line(s, n=80):
    s = " ".join(_IMG_PH.sub("[이미지]", s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def short_key(k):
    parts = (k or "").split(":")
    if len(parts) >= 3 and parts[0] == "c":
        return "%s/%s" % (parts[1][:8], parts[-1][:8])
    return k


def ev_ref(key):
    """카드·사건 키에서 sessionId:uuid 참조를 만든다."""
    if not key:
        return "-"
    if key.startswith("c:"):
        return key[2:]
    return key


# ---- 프로젝트 해석 -----------------------------------------------------------
def resolve_project(con, arg):
    if not arg:
        return None
    projs = {r[0] for r in con.execute("SELECT DISTINCT project FROM files WHERE project IS NOT NULL")}
    if arg in projs:
        return arg
    a = arg.rstrip("/")
    r = con.execute("SELECT project FROM sessions WHERE cwd=? ORDER BY last_ts DESC LIMIT 1", (a,)).fetchone()
    if r and r[0]:
        return r[0]
    r = con.execute("SELECT project FROM cards WHERE cwd=? ORDER BY start_ts DESC LIMIT 1", (a,)).fetchone()
    if r and r[0]:
        return r[0]
    guess = re.sub(r"[/\\.:\s]", "-", a)
    if guess in projs:
        return guess
    cands = [p for p in projs if short_project(p) == arg]
    if len(cands) == 1:
        return cands[0]
    return None


# ---- 묶음 --------------------------------------------------------------------
_FAMILY_RX = re.compile(r"(?:-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?-\d{3,}$")
BUNDLES = [("human", "사람 입력 카드"), ("auto", "자동 실행(claude -p)"), ("no_human", "사람 입력 없음(팀원 세션 등)")]


def project_family(p):
    """끝에 가변 꼬리(uuid·숫자)가 붙은 임시 프로젝트를 한 계열로 접는다. 예: …-batchjob-session-<uuid>-60370 → …-session-*"""
    if not p:
        return p
    return _FAMILY_RX.sub("-*", p)


def bundle_of(human_kind, delivered):
    if not delivered:
        return "ghost"
    if human_kind == "sdk":
        return "auto"
    if human_kind == "no_human":
        return "no_human"
    return "human"


def _fold_counts(projects):
    """프로젝트 목록을 계열로 접어 '이름 n' 을 많은 순으로 한 줄에."""
    c = collections.Counter(short_project(project_family(p)) for p in projects)
    return " · ".join("%s %d" % kv for kv in sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def slash_noarg_counts(con, lo, hi):
    """인자 없는 슬래시 명령(/model 등) user 줄 수(프로젝트별). 사람이 친 입력이지만 카드는 아니다."""
    return dict(con.execute(
        "SELECT project, count(*) FROM events WHERE kind='command' AND ts >= ? AND ts < ? AND fid IN"
        " (SELECT fid FROM files WHERE kind='session') GROUP BY project", (lo, hi)).fetchall())


def slash_local_counts(con, lo, hi):
    """system/local_command 로 적힌 슬래시 명령 줄 수(프로젝트별, 참고). 새 판은 일부 명령을 이 모양으로 적는데,
    사람이 친 것인지(재연결 때 자동으로 적히는지) 기록만으로 가를 수 없어 사람 입력 수에는 넣지 않는다."""
    return dict(con.execute(
        "SELECT project, count(*) FROM events WHERE kind='system' AND sub='local_command' AND text LIKE '<command-name>%'"
        " AND ts >= ? AND ts < ? AND fid IN (SELECT fid FROM files WHERE kind='session') GROUP BY project", (lo, hi)).fetchall())


# ---- day ---------------------------------------------------------------------
SHORT_INPUT_MAX = 4  # 공백·이미지 자리표시를 뺀 글자 수가 이 이하인 사람 입력(이미지 전용 포함)은 이어지면 한 줄로 접는다
LAST_ANS_CHARS = 60
INPUT_CHARS = 38  # 마지막 답을 싣는 대신 입력은 38자, 카드 줄의 소요·도구·키는 뺀다(중앙값 길이를 기준선 이하로)
_BASH_TAG = re.compile(r"<bash-input>([\s\S]*?)</bash-input>")
_CMD_NAME = re.compile(r"<command-name>([\s\S]*?)</command-name>")
_CMD_ARGS = re.compile(r"<command-args>([\s\S]*?)</command-args>")
_IMG_ANY = re.compile(r"\[이미지[^\]]*\]|\[Image #\d+\]")
_WF_SUFFIX = re.compile(r"-wf_[0-9a-f][0-9a-f-]*$")


def card_text(human):
    """day 줄의 사람 입력. '!' 입력은 '! 명령', 슬래시 명령은 태그를 벗긴 '/이름 인자'(/goal 인자가 보이게)."""
    t = (human or "").strip()
    if t.startswith("<bash-input>"):
        m = _BASH_TAG.search(t)
        if m:
            return "! " + m.group(1).strip()
    if t.startswith("<command-name>"):
        n = _CMD_NAME.search(t)
        a = _CMD_ARGS.search(t)
        if n:
            return n.group(1).strip() + ((" " + a.group(1).strip()) if a and a.group(1).strip() else "")
    return t


def is_short_input(human):
    """이미지 전용이거나 글이 SHORT_INPUT_MAX 자 이하인 입력(공백·이미지 자리표시는 세지 않는다)."""
    t = _IMG_ANY.sub(" ", _IMG_PH.sub(" ", card_text(human)))
    return len("".join(t.split())) <= SHORT_INPUT_MAX


def workflow_calls(con, lo, hi):
    """[lo, hi) 에 부른 Workflow 도구의 (sid, ts, 이름). 이름 = workflow_name, 없으면 scriptPath 파일 이름(-wf_<id> 뗌)."""
    names = sorted(parse.WORKFLOW_TOOLS)
    out = []
    for sid, ts, inp in con.execute("SELECT sid, ts, inp FROM tool_uses WHERE name IN (%s) AND ts >= ? AND ts < ?"
                                    % ",".join("?" * len(names)), tuple(names) + (lo, hi)):
        try:
            j = json.loads(inp or "{}")
        except ValueError:
            j = {}
        name = j.get("workflow_name") if isinstance(j.get("workflow_name"), str) else None
        if not name and isinstance(j.get("scriptPath"), str):
            name = _WF_SUFFIX.sub("", os.path.splitext(os.path.basename(j["scriptPath"]))[0])
        if name and name.strip():
            out.append((sid, ts, name.strip()))
    return out


def day(con, date):
    lo, hi = paths.kst_day_bounds_utc(date)
    rows = con.execute("SELECT key, project, start_ts, dur_s, human, n_tools, n_files, human_kind, delivered, n_err, n_sub, n_wf,"
                       " sid, end_ts, last_asst FROM cards WHERE start_ts >= ? AND start_ts < ? ORDER BY start_ts",
                       (lo, hi)).fetchall()
    groups = collections.defaultdict(list)
    for r in rows:
        groups[bundle_of(r[7], r[8])].append(r)
    slash = slash_noarg_counts(con, lo, hi)
    human = groups["human"]
    by = collections.OrderedDict()
    for r in human:
        by.setdefault(r[1], []).append(r)
    for p in sorted(slash):
        by.setdefault(p, [])
    n_slash = sum(slash.values())
    n_local = sum(slash_local_counts(con, lo, hi).values())
    out = ["# %s 작업 타임라인 (%s)" % (date, paths.tz_label_for_date(date)),
           "사람 입력 %d건 = 카드 %d장 + 인자 없는 슬래시 명령 %d건(카드 아님) · 프로젝트 %d개" % (
               len(human) + n_slash, len(human), n_slash, len(by)),
           "따로 묶음: 자동 실행(claude -p) %d장 · 사람 입력 없음(팀원 세션 등) %d장 · 전달 안 된 대기열 %d건" % (
               len(groups["auto"]), len(groups["no_human"]), len(groups["ghost"])),
           "셈 규칙: 사람 입력 = 카드(사람 줄·작업 도중 입력·작업 도중 /goal·! 입력·인자 있는 슬래시) + user 줄의 인자 없는 슬래시."
           " system/local_command 로 적힌 슬래시 %d건은 참고로만 센다(합계에 넣지 않음)." % n_local]
    if not rows and not n_slash:
        out += ["", "이날 카드가 없다."]

    def line(r, extra_proj=False):
        extra = []
        if r[9]:
            extra.append("에러 %d" % r[9])
        if r[10]:
            extra.append("서브 %d" % r[10])
        if r[11]:
            extra.append("워크플로 %d" % r[11])
        return "- %s%s · %s · 소요 %s · 도구 %d · 파일 %d%s · [%s]" % (
            paths.kst_str(r[2], "%H:%M"), (" · " + short_project(r[1])) if extra_proj else "",
            one_line(card_text(r[4]), 70) or "(빈 입력)", fmt_dur(r[3]), r[5] or 0, r[6] or 0,
            (" · " + " · ".join(extra)) if extra else "", short_key(r[0]))

    def answer(r):
        a = one_line(r[14], LAST_ANS_CHARS)
        return (" → " + a) if a else ""

    def cline(r):
        return "- %s %s%s" % (paths.kst_str(r[2], "%H:%M"), one_line(card_text(r[4]), INPUT_CHARS) or "(빈 입력)", answer(r))

    def folded(run):
        return "- %s~%s 짧은 입력 %d장(%s)%s" % (
            paths.kst_str(run[0][2], "%H:%M"), paths.kst_str(run[-1][2], "%H:%M"), len(run),
            "·".join(one_line(card_text(r[4]), 12) or "(빈 입력)" for r in run), answer(run[-1]))

    def first_ts(kv):
        return kv[1][0][2] if kv[1] else "9"

    wf_hi = max([hi] + [r[13] or "" for r in human])
    wf = collections.defaultdict(list)
    for sid, ts, name in workflow_calls(con, lo, wf_hi + "~") if human else []:
        wf[sid].append((ts, name))
    titles = {}
    for proj, rs in sorted(by.items(), key=first_ts):
        k = slash.get(proj, 0)
        out.append("")
        out.append("## %s · 사람 입력 %d (카드 %d + 인자 없는 슬래시 %d) · 소요 합 %s" % (
            short_project(proj), len(rs) + k, len(rs), k, fmt_dur(sum(r[3] or 0 for r in rs))))
        sess = collections.OrderedDict()
        for r in rs:
            sess.setdefault(r[12], []).append(r)
        for sid, cs in sess.items():
            if sid not in titles:
                t = con.execute("SELECT title FROM sessions WHERE sid=?", (sid,)).fetchone()
                titles[sid] = t[0] if t and t[0] else None
            names = []
            for ts, name in sorted(wf.get(sid, [])):
                if name not in names and any((c[2] or "") <= ts <= (c[13] or c[2] or "") for c in cs):
                    names.append(name)
            head = ["### %s" % sid[:8]]
            if titles[sid]:
                head.append(one_line(titles[sid], 30))
            head.append("카드 %d · 소요 %s" % (len(cs), fmt_dur(sum(c[3] or 0 for c in cs))))
            if names:
                head.append("워크플로 " + ", ".join(names))
            out.append(" · ".join(head))
            run = []
            for c in cs + [None]:
                if c is not None and is_short_input(c[4]):
                    run.append(c)
                    continue
                if len(run) >= 2:
                    out.append(folded(run))
                elif run:
                    out.append(cline(run[0]))
                run = []
                if c is not None:
                    out.append(cline(c))
    if groups["no_human"]:
        out += ["", "## 사람 입력 없음(팀원 세션 등, 세션 단위) · %d장" % len(groups["no_human"])]
        out += [line(r, extra_proj=True) for r in groups["no_human"]]
    if groups["auto"]:
        out += ["", "## 자동 실행(claude -p) · %d장 · 소요 합 %s" % (
            len(groups["auto"]), fmt_dur(sum(r[3] or 0 for r in groups["auto"]))),
                "- 프로젝트(계열로 접음): " + _fold_counts(r[1] for r in groups["auto"])]
    if groups["ghost"]:
        out += ["", "## 전달 안 된 대기열(queue-operation 만 있고 전달 줄 없음) · %d건" % len(groups["ghost"])]
        out += ["- %s · %s · %s · [%s]" % (paths.kst_str(r[2], "%H:%M"), short_project(r[1]), one_line(r[4], 70),
                                           short_key(r[0])) for r in groups["ghost"]]
    return "\n".join(out)


# ---- eff ---------------------------------------------------------------------
def _q(vals, p):
    if not vals:
        return None
    v = sorted(vals)
    k = (len(v) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _n(x, d=1):
    if x is None:
        return "-"
    if isinstance(x, float):
        if abs(x) >= 1000:
            return "{:,.0f}".format(x)
        return ("{:.%df}" % d).format(x)
    return "{:,}".format(x)


METRICS = [
    ("dur_min", "소요(분)"), ("n_asst", "응답 수"), ("n_tools", "도구 호출"), ("tok_out", "출력 토큰"),
    ("tok_all", "전체 토큰"), ("n_err", "에러"), ("n_denied", "거절"), ("n_intr", "중단"), ("n_err_repeat", "같은 에러 반복"),
]
FEATURE_LABEL = [("workflow", "워크플로 사용"), ("subagent", "서브에이전트 사용"), ("todo", "TodoWrite 사용"),
                 ("plan", "계획 모드"), ("verify_agent", "검증 에이전트 사용"), ("skill", "스킬 사용")]


def _card_metrics(con, project=None, since=None):
    q = ("SELECT key, project, dur_s, n_asst, n_tools, tok_out, tok_in, tok_cc, tok_cr, n_err, n_denied, n_intr,"
         " n_err_repeat, features, model, err_sigs, sub_tok_out, human_kind, sid, sub_tok_all FROM cards WHERE delivered=1")
    args = []
    if project:
        q += " AND project=?"
        args.append(project)
    if since:
        q += " AND start_ts >= ?"
        args.append(paths.kst_day_bounds_utc(since)[0])
    out = []
    for r in con.execute(q, args):
        f = json.loads(r[13] or "{}")
        out.append({
            "key": r[0], "project": r[1], "dur_min": (r[2] or 0) / 60.0, "n_asst": r[3] or 0, "n_tools": r[4] or 0,
            "tok_out": r[5] or 0, "tok_all": (r[5] or 0) + (r[6] or 0) + (r[7] or 0) + (r[8] or 0),
            "n_err": r[9] or 0, "n_denied": r[10] or 0, "n_intr": r[11] or 0, "n_err_repeat": r[12] or 0,
            "feat": f, "model": r[14] or "(없음)", "sigs": json.loads(r[15] or "[]"), "sub_tok_out": r[16] or 0,
            "bundle": bundle_of(r[17], 1), "sid": r[18], "sub_tok_all": r[19] or 0,
        })
    return out


def _group_row(label, cs):
    n = len(cs)
    if not n:
        return "| %s | 0 |" % label + " - |" * 9
    cells = [label, _n(n)]
    for m in ("dur_min", "n_asst", "n_tools", "tok_out"):
        cells.append(_n(_q([c[m] for c in cs], 0.5)))
    for m in ("n_err", "n_denied", "n_intr"):
        cells.append(_n(sum(c[m] for c in cs) / n, 2))
    cells.append(_n(100.0 * sum(1 for c in cs if c["n_err_repeat"] > 0) / n, 1))
    cells.append(_n(sum(c["sub_tok_out"] for c in cs)))
    return "| " + " | ".join(cells) + " |"


GROUP_HEAD = ("| 구분 | 카드 | 소요 중앙(분) | 응답 수 중앙 | 도구 호출 중앙 | 출력 토큰 중앙 | 에러/카드 | 거절/카드 | 중단/카드 |"
              " 같은 에러 반복 카드(%) | 하위 에이전트 출력 토큰 합 |")
GROUP_SEP = "|" + "---|" * 11


def raw_token_totals(con, project=None, since=None):
    """원문 사건에서 message.id 기준으로 다시 센 출력 토큰(본선=세션 파일, 하위=서브에이전트·워크플로 파일)과 에러 수.
    카드 합과 대조하는 확인용 숫자다. since 가 있으면 그 시각 이후 사건만 센다."""
    q = "SELECT fid, kind FROM files WHERE kind IN ('session','subagent','wf_agent')"
    args = []
    if project:
        q += " AND project=?"
        args.append(project)
    out = collections.Counter()
    lo = paths.kst_day_bounds_utc(since)[0] if since else None
    for fid, kind in con.execute(q, args).fetchall():
        best = {}
        qq = "SELECT msg_id, u_out FROM events WHERE fid=? AND type='assistant' AND msg_id IS NOT NULL"
        aa = [fid]
        if lo:
            qq += " AND ts >= ?"
            aa.append(lo)
        for mid, uo in con.execute(qq, aa):
            best[mid] = max(best.get(mid, 0), uo or 0)
        out["main_out" if kind == "session" else "sub_out"] += sum(best.values())
        if kind == "session":
            qe = "SELECT count(*) FROM tool_results WHERE fid=? AND is_err=1" + (" AND ts >= ?" if lo else "")
            out["main_err"] += con.execute(qe, aa).fetchone()[0]
    return out


def eff(con, project=None, since=None):
    allc = _card_metrics(con, project, since)
    cs = [c for c in allc if c["bundle"] == "human"]
    nb = collections.Counter(c["bundle"] for c in allc)
    out = ["# 효율 지표", "범위: 프로젝트 %s · 시작 %s 이후 · 사람 입력 카드 %d장(따로: 자동 실행 %d장 · 사람 입력 없음 %d장)" % (
        short_project(project) if project else "전체", since or "처음", len(cs), nb["auto"], nb["no_human"]), ""]
    out.append("## 카드 단위 지표(사람 입력 카드)")
    out.append("| 지표 | 중앙값 | 평균 | p90 | 합 |")
    out.append("|---|---|---|---|---|")
    for m, label in METRICS:
        v = [c[m] for c in cs]
        out.append("| %s | %s | %s | %s | %s |" % (label, _n(_q(v, 0.5)), _n(sum(v) / len(v) if v else None),
                                               _n(_q(v, 0.9)), _n(sum(v) if v else None)))
    out.append("")
    out.append("## 묶음별 합계와 원문 대조(원문은 message.id 기준으로 다시 센 값)")
    out.append("| 묶음 | 카드 | 출력 토큰 합 | 전체 토큰 합 | 에러 합 | 하위 에이전트 출력 토큰 합 |")
    out.append("|---|---|---|---|---|---|")
    tot = collections.Counter()
    for b, label in BUNDLES:
        g = [c for c in allc if c["bundle"] == b]
        row = (len(g), sum(c["tok_out"] for c in g), sum(c["tok_all"] for c in g), sum(c["n_err"] for c in g),
               sum(c["sub_tok_out"] for c in g))
        for k, v in zip(("n", "out", "all", "err", "sub"), row):
            tot[k] += v
        out.append("| %s | %s | %s | %s | %s | %s |" % ((label,) + tuple(_n(x) for x in row)))
    out.append("| 합계 | %s | %s | %s | %s | %s |" % tuple(_n(tot[k]) for k in ("n", "out", "all", "err", "sub")))
    raw = raw_token_totals(con, project, since)
    out.append("| 원문 | - | %s | - | %s | %s |" % (_n(raw["main_out"]), _n(raw["main_err"]), _n(raw["sub_out"])))
    out.append("")
    out.append("## 방식별 비교(사람 입력 카드)")
    out.append(GROUP_HEAD)
    out.append(GROUP_SEP)
    for f, label in FEATURE_LABEL:
        yes = [c for c in cs if c["feat"].get(f)]
        no = [c for c in cs if not c["feat"].get(f)]
        out.append(_group_row(label + ": 예", yes))
        out.append(_group_row(label + ": 아니오", no))
    out.append("")
    out.append("## 모델별 비교(사람 입력 카드, 카드에서 가장 많이 쓴 모델)")
    out.append(GROUP_HEAD)
    out.append(GROUP_SEP)
    bym = collections.defaultdict(list)
    for c in cs:
        bym[c["model"]].append(c)
    for mname, g in sorted(bym.items(), key=lambda kv: -len(kv[1])):
        out.append(_group_row(mname, g))
    if not project:
        out.append("")
        out.append("## 프로젝트별 비교(사람 입력 카드, 임시 프로젝트는 계열로 접음) 및 묶음")
        out.append(GROUP_HEAD)
        out.append(GROUP_SEP)
        byp = collections.defaultdict(list)
        for c in cs:
            byp[project_family(c["project"])].append(c)
        ranked = sorted(byp.items(), key=lambda kv: -len(kv[1]))
        for pname, g in ranked[:10]:
            out.append(_group_row(short_project(pname), g))
        rest = [c for _, g in ranked[10:] for c in g]
        if rest:
            out.append(_group_row("그 밖 프로젝트 %d개" % (len(ranked) - 10), rest))
        for b, label in BUNDLES[1:]:
            g = [c for c in allc if c["bundle"] == b]
            if g:
                out.append(_group_row("%s(프로젝트 %d개)" % (label, len({project_family(c["project"]) for c in g})), g))
    out.append("")
    out.append("## 반복된 에러 서명 상위 10(사람 입력 카드)")
    out.append("| 서명 | 카드 수 | 발생 합 | 첫 줄 |")
    out.append("|---|---|---|---|")
    agg = {}
    for c in cs:
        for sig, n, line in c["sigs"]:
            a = agg.setdefault(sig, [0, 0, line])
            a[0] += 1
            a[1] += n
    for sig, (nc, nn, line) in sorted(agg.items(), key=lambda kv: (-kv[1][1], kv[0]))[:10]:
        out.append("| sig:%s | %d | %d | %s |" % (sig, nc, nn, one_line(line, 70).replace("|", "\\|")))
    return "\n".join(out)


# ---- resume ------------------------------------------------------------------
def _cmd_head(cmd, n=3):
    return " ".join((cmd or "").split()[:n])


# 종료 코드 1 이 '실패'가 아니라 '찾은 것 없음·거짓·다름'인 명령(grep 무일치, test 거짓, diff 다름, find 일부 접근 불가)
_NOMATCH_CMDS = {"grep", "egrep", "fgrep", "zgrep", "rg", "ag", "ack", "find", "diff", "cmp", "test", "[", "[[", "pgrep",
                 "which", "comm", "lsof"}
# 앞 명령의 출력만 거르는 명령(실패할 일이 거의 없다)과 상태를 바꾸지 않는 명령
_FILTER_CMDS = {"head", "tail", "wc", "sort", "uniq", "cut", "tr", "awk", "sed", "cat", "column", "nl", "jq", "less",
                "more", "cd", "echo", "printf", "true", ":", "ls", "stat", "file", "du", "df", "pwd", "basename", "dirname",
                "realpath", "readlink", "ps"}
_HARD_ERR_RX = re.compile(r"No such file or directory|Permission denied|command not found|Traceback \(most recent call"
                          r"|fatal:|syntax error|Operation not permitted|Is a directory|Not a directory|cannot access",
                          re.I)


def nomatch_exit(text, command):
    """Bash 도구 에러가 '종료 코드 1 + 찾은 것 없음'인가. 판정 규칙(모두 만족):
    결과 첫 줄이 'Exit code 1' · 결과에 실패 표시(No such file·Permission denied·Traceback 등)가 없음 ·
    명령의 마지막 문장(; 와 줄바꿈으로 나눈 것, 종료 코드를 정한다)이 grep·find·test·diff 류를 하나 이상 쓰고 나머지는
    거르기·이동·출력 명령(head·wc·sed·cd·echo 등)뿐 · 파일로 쓰는 리디렉션(> 파일)·sed -i·heredoc·set -e 없음."""
    import shlex
    t = (text or "").lstrip()
    first, _, rest = t.partition("\n")
    if first.strip() != "Exit code 1" or _HARD_ERR_RX.search(rest):
        return False
    c = command or ""
    if not c or len(c) >= 2000 or "<<" in c or re.search(r"\bset\s+-[a-z]*e", c):
        return False
    try:
        toks = list(shlex.shlex(c.replace("\n", " ; "), posix=True, punctuation_chars=True))
    except ValueError:
        return False
    stmts = [[]]
    for tk in toks:
        if tk in (";", "&", ";;"):
            stmts.append([])
        else:
            stmts[-1].append(tk)
    last = [x for x in stmts if x]
    if not last:
        return False
    last = last[-1]
    cmds = [[]]
    i = 0
    while i < len(last):
        tk = last[i]
        if tk in ("|", "&&", "||", "|&"):
            cmds.append([])
        elif tk in (">", ">>", ">|", "&>"):
            nxt = last[i + 1] if i + 1 < len(last) else ""
            if nxt != "/dev/null":
                return False
            i += 1
        elif tk in (">&", "<"):
            i += 1
        else:
            cmds[-1].append(tk)
        i += 1
    seen_nomatch = False
    for cw in cmds:
        words = [w for w in cw if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w) and not w.isdigit()]
        if not words:
            continue
        prog = words[0].rsplit("/", 1)[-1]
        if prog == "git" and len(words) > 1 and words[1] in ("grep", "diff"):
            seen_nomatch = True
        elif prog in _NOMATCH_CMDS:
            seen_nomatch = True
        elif prog in _FILTER_CMDS:
            if prog == "sed" and any(w.startswith("-i") for w in words[1:]):
                return False
        else:
            return False
    return seen_nomatch


# 하네스 차단·거부(사용자·권한 거부, sleep 차단, 자동 모드 분류기, 훅 차단): 작업 에러가 아니라 에러 절에서 뺀다
_HARNESS_RX = re.compile(r"Blocked: sleep|Permission (?:to use|denied)|permission denied|denied by|doesn't want to proceed|"
                         r"user (?:rejected|denied)|Auto mode|auto mode|classifier|requires approval|"
                         r"A hook blocked|Operation stopped by hook|tool use was rejected", re.I)
ERR_MAX_AGE_S = 24 * 3600
RECENT_S = 2 * 3600


def _age_s(ts, now):
    a, b = paths.parse_ts(ts), paths.parse_ts(now)
    if not a or not b:
        return None
    return (b - a).total_seconds()


def unresolved_error(con, sid, now=None):
    """세션의 마지막 미해결 에러. 규칙: 에러 뒤에 같은 도구·같은 대상(Bash 는 명령 앞 3낱말, 파일 도구는 경로)의
    성공 결과가 같은 세션에 없으면 미해결로 본다. Bash 의 '종료 코드 1 + 찾은 것 없음'(nomatch_exit)은 에러가 아니라
    결과 없음으로 보고 성공처럼 센다. 하네스 차단·거부(권한·자동 모드·훅·sleep 차단)는 에러로 보지 않고 건너뛴다.
    now 가 있으면 그보다 24시간 넘게 지난 에러는 내지 않는다."""
    # 결과 본문(events.text)은 큰 열이라 에러 줄에서만 따로 읽는다(차가운 첫 실행 시간)
    rows = con.execute(
        "SELECT r.tuid, r.is_err, r.err_sig, r.err_line, r.ts, r.off, u.name, u.cmd, u.fpath, u.inp, r.ekey"
        " FROM tool_results r LEFT JOIN tool_uses u ON u.tuid=r.tuid"
        " WHERE r.sid=? AND r.fid IN (SELECT fid FROM files WHERE sid=? AND kind='session') ORDER BY r.off",
        (sid, sid)).fetchall()
    later_ok = collections.defaultdict(int)
    res = None
    for r in reversed(rows):
        tuid, is_err, sig, line, ts, off, name, cmd, fpath, inp, ekey = r
        rtext = None
        if is_err:
            t = con.execute("SELECT text FROM events WHERE key=?", (ekey,)).fetchone()
            rtext = t[0] if t else None
        target = (name, _cmd_head(cmd) if name == "Bash" else (fpath or ""))
        if is_err and name == "Bash":
            try:
                full = json.loads(inp or "{}").get("command")
            except (ValueError, AttributeError):
                full = None
            if nomatch_exit(rtext, full):
                is_err = 0
        if not is_err:
            later_ok[target] += 1
            continue
        if _HARNESS_RX.search(line or "") or _HARNESS_RX.search((rtext or "")[:2000]):
            continue
        if now:
            age = _age_s(ts, now)
            if age is not None and age > ERR_MAX_AGE_S:
                break
        if later_ok.get(target, 0) == 0:
            res = {"ts": ts, "tool": name, "cmd": cmd, "fpath": fpath, "line": line, "sig": sig}
            break
    return res


def _quote(txt, nchar=600, nline=15):
    """'> ' 인용 줄. 글자 상한과 줄 상한을 모두 적용한 뒤에 자름 표시를 붙인다(표시가 상한에 묻히지 않게)."""
    t = (txt or "").strip()
    body = t[:nchar]
    lines = [ln for ln in body.splitlines() if ln.strip()]
    shown = lines[:nline]
    out = ["> " + ln for ln in shown]
    total = len([ln for ln in t.splitlines() if ln.strip()])
    if len(t) > nchar or len(lines) > nline:
        out.append("> …(잘림: 전체 %d자 %d줄 중 앞 %d줄)" % (len(t), total, len(shown)))
    return out


def _db_last_req(con, sid):
    return con.execute("SELECT key, start_ts, human FROM cards WHERE sid=? AND delivered=1 AND human_kind NOT IN"
                       " ('sdk','no_human') ORDER BY seq DESC LIMIT 1", (sid,)).fetchone()


def _db_last_ans(con, sid):
    return con.execute("SELECT text, ts, key FROM events WHERE sid=? AND kind='assistant' AND sub='text' AND fid IN"
                       " (SELECT fid FROM files WHERE sid=? AND kind='session') ORDER BY off DESC LIMIT 1",
                       (sid, sid)).fetchone()


def _db_compact(con, sid):
    """세션의 마지막 압축 요약에서 Current Work·Next Step 절."""
    # compact_sections 의 키(ekey = 'sid:uuid') 범위로 찾는다(events 를 세션 전체로 훑지 않는다)
    r = con.execute("SELECT e.key, e.ts FROM (SELECT DISTINCT ekey FROM compact_sections WHERE ekey>=? AND ekey<?) c"
                    " JOIN events e ON e.key=c.ekey ORDER BY e.ts DESC LIMIT 1", (sid + ":", sid + ";")).fetchone()
    if not r:
        return None
    secs = con.execute("SELECT title, body FROM compact_sections WHERE ekey=? ORDER BY n", (r[0],)).fetchall()
    return {"ts": r[1], "key": r[0], "sections": [{"title": t, "body": b} for t, b in secs]}


_CW_RX = re.compile(r"current work|현재 작업", re.I)
_NS_RX = re.compile(r"next step|다음 (단계|할 일)", re.I)


def _compact_lines(cp, n=300):
    out = []
    for rx, label in ((_CW_RX, "Current Work"), (_NS_RX, "Next Step")):
        for s in cp.get("sections") or []:
            if rx.search(s.get("title") or ""):
                out.append("- %s: %s" % (label, one_line(s.get("body"), n)))
                break
    return out


def _session_state(con, sid):
    """세션의 마지막 요청·답·압축 요약. 모두 DB(수집 때 가린 값)에서만 읽는다."""
    st = {"sid": sid, "req": None, "ans": None, "compact": None, "last_ts": None}
    r = _db_last_req(con, sid)
    if r:
        st["req"] = {"key": ev_ref(r[0]), "ts": r[1], "text": r[2] or ""}
    a = _db_last_ans(con, sid)
    if a:
        st["ans"] = {"key": a[2], "ts": a[1], "text": a[0] or ""}
    st["compact"] = _db_compact(con, sid)
    st["last_ts"] = max([x["ts"] for x in (st["req"], st["ans"]) if x and x["ts"]] or [None], key=lambda v: v or "")
    return st


_SID_FILE_RX = re.compile(r"^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


def pending_sessions(con, project, now, exclude=(), limit=5):
    """마지막 수집 뒤에 원문이 바뀐 세션(수집이 아직 못 담은 활동). 원문 파일은 열지 않는다.
    프로젝트 폴더의 이름 목록과 파일 크기·시각(stat)만 쓴다. 마지막 수집 뒤에 바뀐 파일 중에서, 수집이 아는 파일은
    크기가 수집 때와 다를 때만(수집기도 크기가 같으면 새 줄이 없다고 보고 건너뛴다, 그래서 files.mtime 은 낡을 수 있다),
    모르는 파일은 있기만 하면 넣는다. 이어 하기 후보 창(최근 2시간 + 30분) 안에 바뀐 것만 본다.
    (마지막 수집 시각, [(sid, 바뀐 시각 UTC ISO)], 상한 밖 수)."""
    import datetime
    r = con.execute("SELECT max(at) FROM runs WHERE cmd LIKE 'ingest%'").fetchone()
    last = r[0] if r else None
    t_now = paths.parse_ts(now)
    t_last = paths.parse_ts(last)
    if not last or not t_now or not t_last:
        return last, [], 0
    lo = t_now - datetime.timedelta(seconds=RECENT_S + 1800)
    hi = t_now + datetime.timedelta(seconds=60)  # 측정용 과거 시각(now)보다 뒤에 바뀐 파일은 그 시각에 몰랐던 것이다
    known = {}
    for path, size in con.execute("SELECT path, size FROM files WHERE project=? AND kind='session'", (project,)):
        known[os.path.basename(path or "")] = size
    found = []
    try:
        with os.scandir(os.path.join(paths.claude_dir(), "projects", project)) as it:
            for ent in it:
                m = _SID_FILE_RX.match(ent.name)
                if not m or m.group(1) in exclude:
                    continue
                try:
                    st = ent.stat(follow_symlinks=False)
                except OSError:
                    continue
                d = datetime.datetime.fromtimestamp(st.st_mtime, tz=datetime.timezone.utc)
                if d < lo or d > hi or d <= t_last:
                    continue
                if ent.name in known and known[ent.name] == st.st_size:
                    continue
                found.append((m.group(1), d.strftime("%Y-%m-%dT%H:%M:%S.000Z")))
    except OSError:
        return last, [], 0
    found.sort(key=lambda x: x[1], reverse=True)
    return last, found[:limit], max(0, len(found) - limit)


def candidate_sessions(con, project, now, exclude=(), limit=8):
    """이어 하기 후보: 최근 2시간(+수집 낡음 여유 30분) 안에 사람 입력 카드나 활동이 있던 세션, 없으면 마지막 하나."""
    lo = _shift_iso(now, -(RECENT_S + 1800))
    rows = con.execute("SELECT c.sid, max(c.start_ts) m FROM cards c WHERE c.project=? AND c.delivered=1 AND c.human_kind"
                       " NOT IN ('sdk','no_human') GROUP BY c.sid ORDER BY m DESC LIMIT 40", (project,)).fetchall()
    act = {r[0]: r[1] for r in con.execute("SELECT sid, last_ts FROM sessions WHERE project=? AND last_ts>=?", (project, lo))}
    out = []
    for sid, m in rows:
        if sid in exclude:
            continue
        if (m or "") >= lo or sid in act or not out:
            out.append(sid)
        if len(out) >= limit:
            break
    return out


def _shift_iso(ts, seconds):
    import datetime
    d = paths.parse_ts(ts)
    if not d:
        return ts
    d = d + datetime.timedelta(seconds=seconds)
    return d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def running_workflows(con, project, now, sids=None):
    """프로젝트 세션에서 24시간 안에 띄운 워크플로 중 끝남 알림이 없는 것(진행 중)과 2시간 안에 끝난 것."""
    lo = _shift_iso(now, -ERR_MAX_AGE_S)
    # 워크플로를 띄운 카드(n_wf>0)에서 시작해 그 파일의 뒤쪽만 (fid, off) 색인으로 훑는다
    rows = []
    starts = {}
    for sid, ekey in con.execute("SELECT sid, first_ekey FROM cards WHERE project=? AND start_ts>=? AND n_wf>0"
                                 " ORDER BY start_ts", (project, lo)).fetchall():
        e = con.execute("SELECT fid, off FROM events WHERE key=?", (ekey,)).fetchone()
        if e and (e[0] not in starts or e[1] < starts[e[0]][1]):
            starts[e[0]] = (sid, e[1])
    for fid, (sid, off0) in starts.items():
        for ts, inp, rkey, uoff in con.execute(
                "SELECT u.ts, u.inp, r.ekey, u.off FROM tool_uses u JOIN tool_results r ON r.tuid=u.tuid"
                " WHERE u.fid=? AND u.off>=? AND u.name='Workflow' AND u.ts>=?", (fid, off0, lo)).fetchall():
            t = con.execute("SELECT text FROM events WHERE key=?", (rkey,)).fetchone()
            rows.append((sid, ts, inp, t[0] if t else "", fid, uoff))
    rows.sort(key=lambda r: r[1] or "", reverse=True)
    notes = {}
    out = []
    for sid, ts, inp, rtext, fid, uoff in rows[:20]:
        m = re.search(r"Task ID: (\w+)", rtext or "")
        w = re.search(r"workflows/(wf_[0-9a-f][0-9a-f-]{5,})", rtext or "")
        if not m or not w:
            continue
        if fid not in notes:
            notes[fid] = [(t or "", x or "") for t, x in con.execute(
                "SELECT ts, text FROM events WHERE fid=? AND off>=? AND kind='task_note'", (fid, starts[fid][1]))]
        tag = "<task-id>%s</task-id>" % m.group(1)
        done = [(t, x) for t, x in notes[fid] if tag in x and t >= ts]
        try:
            name = json.loads(inp or "{}").get("workflow_name") or ""
        except (ValueError, AttributeError):
            name = ""
        if done:
            t, x = max(done)
            stt = re.search(r"<status>(\w+)</status>", x)
            age = _age_s(t, now)
            if age is None or age > RECENT_S:
                continue
            state = "끝남(%s) %s" % (stt.group(1) if stt else "?", paths.kst_str(t, "%H:%M"))
        else:
            state = "진행 중"
        out.append("- %s · %s · 시작 %s · 세션 %s · %s" % (w.group(1), one_line(name, 40) or "-",
                                                     paths.kst_str(ts, "%m-%d %H:%M"), sid[:8], state))
    return out


def resume(con, project, max_chars=4000, exclude=(), now=None):
    """이어 하기. 요청·답·압축 요약은 DB(수집 때 가린 값)에서만 낸다. exclude: 뺄 세션(새 세션 자신).
    수집 뒤 활동은 원문 파일의 이름과 시각만 보고 한 줄로 알린다(pending_sessions, 내용은 읽지 않는다)."""
    now = now or paths.now_utc_iso()
    sids = candidate_sessions(con, project, now, exclude)
    head = ["# 이어 하기: %s" % short_project(project),
            "pwiki resume · 만든 시각 %s · 결정론 사실만(LLM 요약 없음)" % paths.kst_str_z(now)]
    last, pend, more = pending_sessions(con, project, now, exclude)
    if pend:
        head.append("마지막 수집(%s) 뒤 활동한 세션(그 활동은 아래에 없음, 원문 내용은 읽지 않음): %s%s" % (
            paths.kst_str_z(last, "%H:%M"), ", ".join("%s %s" % (sid[:8], paths.kst_str(ts, "%H:%M")) for sid, ts in pend),
            " 외 %d개" % more if more else ""))
    states = [_session_state(con, s) for s in sids]
    drop = set(exclude or ())
    states = [s for s in states if s["req"]]
    states.sort(key=lambda s: s["req"]["ts"] or "", reverse=True)
    if not states:
        return "\n".join(head + ["", "이 프로젝트의 작업 카드가 없다."])
    lo2 = _shift_iso(now, -RECENT_S)
    top = states[0]
    others = [s for s in states[1:] if (s["req"]["ts"] or "") >= lo2 or (s["last_ts"] or "") >= lo2]
    sess = con.execute("SELECT title, first_ts, last_ts, n_cards, cwd FROM sessions WHERE sid=?", (top["sid"],)).fetchone()
    title, fts, lts, ncards, cwd = sess or (None, None, None, 0, None)
    secs = [("마지막 세션(가장 최근 사람 입력 기준)", [
        "- 세션: %s" % top["sid"],
        "- 제목: %s" % (one_line(title, 100) if title else "(없음)"),
        "- 기간: %s ~ %s · 카드 %d장" % (paths.kst_str(fts), paths.kst_str_z(max(lts or "", top["last_ts"] or "") or None),
                                         ncards or 0),
        "- cwd: %s" % (cwd or "-"),
    ])]
    rq = top["req"]
    secs.append(("마지막 사람 요청", ["- %s · [%s]" % (paths.kst_str_z(rq["ts"]), rq["key"])] + _quote(rq["text"], 500, 12)))
    if top["ans"]:
        an = top["ans"]
        secs.append(("마지막 답 앞부분", ["- %s · [%s]" % (paths.kst_str_z(an["ts"]), an["key"])] + _quote(an["text"], 500, 12)))
    if top["compact"]:
        cl = _compact_lines(top["compact"])
        if cl:
            secs.append(("마지막 압축 요약(%s)" % paths.kst_str_z(top["compact"]["ts"]), cl))
    if others:
        lines = []
        for s in others:
            t = con.execute("SELECT title FROM sessions WHERE sid=?", (s["sid"],)).fetchone()
            lines.append("- %s · 요청 %s · %s" % (s["sid"][:8], paths.kst_str(s["req"]["ts"], "%H:%M"),
                                                one_line(t[0] if t and t[0] else "", 40) or "-"))
            lines.append("  요청: %s" % one_line(s["req"]["text"], 110))
            if s["ans"] and (s["ans"]["ts"] or "") >= (s["req"]["ts"] or ""):
                lines.append("  답: %s" % one_line(s["ans"]["text"], 110))
        secs.append(("최근 2시간 다른 세션(%d개, 사람 입력 최신순)" % len(others), lines))
    wf = running_workflows(con, project, now)
    secs.append(("워크플로(진행 중·2시간 안 끝남)", wf or ["- 없음(24시간 안에 띄운 것 기준)"]))
    tw = con.execute("SELECT todos, start_ts, key FROM cards WHERE project=? AND todos IS NOT NULL ORDER BY start_ts DESC"
                     " LIMIT 1", (project,)).fetchone()
    if tw:
        todos = json.loads(tw[0] or "[]")
        open_items = [t for t in todos if (t or {}).get("status") != "completed"]
        if open_items:
            lines = ["- 기준: %s 카드 [%s] · 전체 %d · 미완 %d" % (paths.kst_str_z(tw[1]), ev_ref(tw[2]), len(todos),
                                                                 len(open_items))]
            lines += ["- [%s] %s" % (t.get("status"), one_line(t.get("content"), 120)) for t in open_items]
            secs.append(("미완 할 일(마지막 TodoWrite)", lines))
    ue = unresolved_error(con, top["sid"], now)
    if ue:
        secs.append(("해결 안 된 마지막 에러", [
            "- %s · 도구 %s · sig:%s" % (paths.kst_str_z(ue["ts"]), ue["tool"] or "?", ue["sig"] or "-"),
            "- 대상: %s" % one_line(ue["cmd"] or ue["fpath"] or "-", 140),
            "- 첫 줄: %s" % one_line(ue["line"], 200),
            "- 판정 규칙: 뒤에 같은 도구·같은 대상(Bash 는 명령 앞 3낱말)의 성공이 이 세션에 없음"
            " (찾은 것 없음·하네스 차단·24시간 지난 것은 뺌)",
        ]))
    else:
        secs.append(("해결 안 된 마지막 에러", ["- 없음(24시간 안, 하네스 차단·거부는 뺌)"]))
    dd = paths.kst_date(top["req"]["ts"])
    if dd:
        lo, hi = paths.kst_day_bounds_utc(dd)
        rows = con.execute("SELECT start_ts, human, sid FROM cards WHERE project=? AND delivered=1 AND human_kind NOT IN"
                           " ('sdk','no_human') AND start_ts>=? AND start_ts<? ORDER BY start_ts DESC",
                           (project, lo, hi)).fetchall()
        rows = [r for r in rows if r[2] not in drop]
        secs.append(("그날 작업(%s, 카드 %d장, 최신부터)" % (dd, len(rows)),
                     ["- %s %s · %s" % (paths.kst_str(r[0], "%H:%M"), r[2][:8], one_line(r[1], 60)) for r in rows]))
    recent = [r for r in con.execute("SELECT key, start_ts, human, dur_s, n_tools, n_files, n_err, delivered, human_kind, sid"
                                     " FROM cards WHERE project=? ORDER BY start_ts DESC LIMIT 8", (project,)).fetchall()
              if r[9] not in drop][:5]
    mark = {"auto": " (자동 실행)", "no_human": " (사람 입력 없음)", "ghost": " (전달 안 된 대기열)", "human": ""}
    secs.append(("최근 카드", ["- %s · %s%s · 소요 %s · 도구 %d · 파일 %d · 에러 %d · [%s]" % (
        paths.kst_str(r[1], "%m-%d %H:%M"), one_line(r[2], 60), mark[bundle_of(r[8], r[7])], fmt_dur(r[3]), r[4] or 0,
        r[5] or 0, r[6] or 0, short_key(r[0])) for r in reversed(recent)]))
    return assemble(head, secs, max_chars)


def assemble(head, secs, max_chars):
    """절을 순서대로 붙이다가 상한을 넘으면 그 자리에서 자르고, 잘렸다고 적는다. 결과 길이는 상한 이하다."""
    out = list(head)
    used = sum(len(x) + 1 for x in out)
    dropped_lines = 0
    dropped_secs = []
    reserve = 120
    for title, lines in secs:
        h = "\n## " + title
        if used + len(h) + 1 + reserve > max_chars:
            dropped_secs.append(title)
            dropped_lines += len(lines)
            continue
        out.append(h)
        used += len(h) + 1
        for i, ln in enumerate(lines):
            if used + len(ln) + 1 + reserve > max_chars:
                rest = len(lines) - i
                dropped_lines += rest
                note = "- (잘림: 이 절 %d줄 생략)" % rest
                out.append(note)
                used += len(note) + 1
                break
            out.append(ln)
            used += len(ln) + 1
    if dropped_lines or dropped_secs:
        tail = "\n_잘림: 상한 %d자 · 생략 %d줄%s_" % (max_chars, dropped_lines,
                                                   (" · 빠진 절: " + ", ".join(dropped_secs)) if dropped_secs else "")
        out.append(tail[:reserve])
    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars - 12] + "\n_잘림_"
    return text


# ---- search ------------------------------------------------------------------
def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _tokens(query):
    out = []
    for t in (query or "").split():
        t = t.strip("\"'`.,;:!?()[]{}<>")
        if t and t.lower() not in [x.lower() for x in out]:
            out.append(t)
    return out


# 한국어 낱말 끝의 조사·어미(고정 목록, 긴 것부터). 떼고 남은 어간이 2글자 이상일 때만 뗀다.
_KO_SUFFIX = sorted(["은", "는", "이", "가", "을", "를", "에", "의", "도", "로", "와", "과", "지", "다", "요",
                     "에서", "에게", "으로", "까지", "부터", "처럼", "보다", "이나", "에는", "라는", "이라",
                     "에서는", "으로는", "이라는", "하는", "하고", "하면", "해서", "했다", "했지", "했던", "했고",
                     "한다", "합니다", "된다", "되는", "됐다", "되어", "했는데", "하는데"], key=len, reverse=True)


def ko_stem(t):
    """한국어 낱말의 어간(끝 조사·어미 하나를 뗀 것). 해당이 없으면 None."""
    if not re.search(r"[가-힣]$", t):
        return None
    for suf in _KO_SUFFIX:
        if t.endswith(suf) and len(t) - len(suf) >= 2:
            return t[:-len(suf)]
    return None


PER_SESSION = 3
# 기본 검색에서 빼는 원천: 하위 에이전트·워크플로 파일(<세션>/subagents/ 아래 줄 파일과 meta, <세션>/workflows/ 아래 문서).
# 같은 질문을 되풀이해 적은 검증·팀원 기록이 상위를 차지하지 않게 한다. include_all=True(--all)면 넣는다.
AUX_FILE_KINDS = ("subagent", "wf_agent", "journal")
AUX_DOC_KINDS = ("doc:agent_meta", "doc:wf_run", "doc:wf_script")


def search(con, query, project=None, since=None, kind=None, limit=20, pool=3000, until=None, exclude=None,
           per_session=PER_SESSION, include_all=False):
    """낱말 OR 검색. 낱말마다 모양 = 낱말 자신 + (한국어면) 끝 조사·어미를 뗀 어간.
    후보: 3글자 이상 모양은 FTS5 trigram MATCH 를 OR 로 묶어 bm25 순 pool 개, 3글자 미만 모양은 LIKE OR(모두).
    순위: _rank_key. bm25(FTS 후보만, 작을수록 관련. LIKE 로만 걸린 행은 0 이라 뒤)에 일치 낱말 비율의 제곱을 곱한
    점수(낱말의 모양 중 하나라도 본문에 있으면 일치) → 일치 수 → 최근(시각 없는 행은 뒤).
    3글자 미만 낱말은 걸러 내지 않고 일치 낱말 수에만 더한다(가점).
    고르기: 같은 세션은 순위 순으로 per_session 건까지만 먼저 싣고(검색어를 되풀이해 적은 작업 세션 하나가 상위를 다
    채우지 않게), 자리가 남으면 넘긴 행을 순위 순으로 채운다. per_session=0 이면 끈다.
    거르기: until(그날 KST 까지 포함), exclude(세션 id 또는 앞부분 목록), 기본은 하위 에이전트·워크플로 파일 행 제외
    (include_all=True 면 포함)."""
    toks = _tokens(query)
    if not toks:
        return {"method": None, "rows": [], "long": [], "short": [], "candidates": 0, "per_session": per_session,
                "include_all": bool(include_all)}
    forms = []
    for t in toks:
        f = [t.lower()]
        st = ko_stem(t)
        if st and st.lower() not in f:
            f.append(st.lower())
        forms.append(f)
    long_f = sorted({x for f in forms for x in f if len(x) >= 3})
    short_f = sorted({x for f in forms for x in f if len(x) < 3})
    filt, fargs = [], []
    if project:
        filt.append("fts.project = ?")
        fargs.append(project)
    if since:
        filt.append("fts.ts >= ?")
        fargs.append(paths.kst_day_bounds_utc(since)[0])
    if until:
        filt.append("fts.ts < ?")
        fargs.append(paths.kst_day_bounds_utc(until)[1])
    for x in exclude or []:
        filt.append("(fts.sid IS NULL OR fts.sid NOT LIKE ? ESCAPE '\\')")
        fargs.append(_like_escape(x) + "%")
    if kind:
        if kind.endswith(":") or kind in ("doc", "att"):
            filt.append("fts.kind LIKE ?")
            fargs.append(kind.rstrip(":") + ":%")
        else:
            filt.append("fts.kind = ?")
            fargs.append(kind)
    if not include_all:
        filt.append("fts.kind NOT IN (%s)" % ",".join("?" * len(AUX_DOC_KINDS)))
        fargs.extend(AUX_DOC_KINDS)
        filt.append("NOT EXISTS (SELECT 1 FROM events e WHERE e.key = fts.key AND e.fid IN"
                    " (SELECT fid FROM files WHERE kind IN (%s)))" % ",".join("?" * len(AUX_FILE_KINDS)))
        fargs.extend(AUX_FILE_KINDS)
    cands = {}
    if long_f:
        sql = ("SELECT fts.key, fts.kind, fts.project, fts.ts, fts.sid, fts.body, bm25(fts) FROM fts WHERE fts MATCH ?"
               + "".join(" AND " + w for w in filt) + " ORDER BY bm25(fts) LIMIT ?")
        for r in con.execute(sql, [" OR ".join('"%s"' % t.replace('"', '""') for t in long_f)] + fargs + [int(pool)]):
            cands[r[0]] = r
    if short_f:
        sql = ("SELECT fts.key, fts.kind, fts.project, fts.ts, fts.sid, fts.body, 0.0 FROM fts WHERE ("
               + " OR ".join("fts.body LIKE ? ESCAPE '\\'" for _ in short_f) + ")" + "".join(" AND " + w for w in filt))
        for r in con.execute(sql, ["%" + _like_escape(t) + "%" for t in short_f] + fargs):
            cands.setdefault(r[0], r)
    method = "+".join(x for x in (("fts-or" if long_f else ""), ("like-or" if short_f else "")) if x)
    scored = []
    for key, k, proj, ts, sid, body, rank in cands.values():
        low = (body or "").lower()
        n = sum(1 for f in forms if any(x in low for x in f))
        scored.append((n, rank or 0.0, ts or "", key, k, proj, sid, body))
    scored.sort(key=lambda x: _rank_key(x, len(forms)))
    picked = _diversify(scored, int(limit), int(per_session or 0))
    rows = []
    for n, rank, ts, key, k, proj, sid, body in picked:
        rows.append({"key": key, "kind": k, "project": proj, "ts": ts, "sid": sid,
                     "snippet": _snip(body, [x for f in forms for x in f]),
                     "method": "%s(%d/%d)" % (method, n, len(toks)), "matched": n})
    return {"method": method, "rows": rows, "long": long_f, "short": short_f, "candidates": len(cands),
            "per_session": int(per_session or 0), "include_all": bool(include_all)}


def _is_compact(k, body):
    return "compact" in (k or "") or (body or "")[:80].startswith("This session is being continued")


def _rank_key(x, ntok):
    """x = (n, bm25, ts, key, kind, project, sid, body), ntok = 검색 낱말 수.
    점수 = bm25 × (일치 낱말 수 / 낱말 수)². bm25 는 음수라 작을수록 앞이다. 낱말을 다 맞춘 행은 bm25 그대로,
    일부만 맞춘 행은 비율의 제곱만큼 약해진다(bm25 만 보면 1~2낱말만 맞는 행이, 일치 수만 보면 긴 압축 요약이 앞선다).
    압축 요약 행은 일치 수를 하나 덜 센다(길어서 낱말이 많이 걸릴 뿐 답 자리가 아니다).
    LIKE 로만 걸린 행은 bm25 가 0 이라 FTS 행 뒤다. 같은 점수면 일치 수 → 시각 있는 행 → 최근 → 키."""
    n, rank, ts, key, k, body = x[0], x[1], x[2], x[3], x[4], x[7]
    ne = max(0, n - (1 if _is_compact(k, body) else 0))
    score = (rank or 0.0) * (float(ne) / max(1, ntok)) ** 2
    return (score, -n, 0 if ts else 1, _neg_ts(ts), key)


def _diversify(scored, limit, cap):
    if cap <= 0:
        return scored[:limit]
    out, held = [], []
    used = collections.Counter()
    for r in scored:
        g = r[6] or r[3]  # 세션이 없는 행(문서)은 저마다 한 무리
        if used[g] >= cap:
            held.append(r)
            continue
        used[g] += 1
        out.append(r)
        if len(out) >= limit:
            return out
    return out + held[:limit - len(out)]


def _neg_ts(ts):
    """시각 문자열을 내림차순 정렬 키로(ISO 문자열의 각 글자를 뒤집은 값)."""
    return "".join(chr(0x10FFFF - ord(ch)) for ch in (ts or ""))


def _snip(body, toks, width=70):
    b = body or ""
    low = b.lower()
    pos = -1
    for t in sorted(toks, key=len, reverse=True):
        pos = low.find(t.lower())
        if pos >= 0:
            break
    if pos < 0:
        return one_line(b, width * 2)
    s = max(0, pos - width)
    e = min(len(b), pos + width)
    return ("…" if s else "") + " ".join(b[s:e].split()) + ("…" if e < len(b) else "")


def format_search(res, query):
    out = ["# 검색: %s" % query,
           "일치 방식: %s (3글자 이상 모양 %s → FTS trigram MATCH OR, 3글자 미만 모양 %s → LIKE OR. "
           "모양 = 낱말 + 한국어 어간. 순위: bm25 → 일치 낱말 수 → 최근, 시각 없는 행은 뒤)" % (
               res.get("method"), res.get("long"), res.get("short")),
           ("같은 세션은 순위 순으로 %d건까지 먼저 싣고 남는 자리를 채운다(--per-session 0 이면 끔)" % res["per_session"])
           if res.get("per_session") else "세션별 상한 없음",
           "범위: 하위 에이전트·워크플로 파일 행 포함" if res.get("include_all")
           else "범위: 하위 에이전트·워크플로 파일 행 제외(--all 이면 포함)",
           "결과 %d건(후보 %d건 중) · 전문: pwiki show <키>" % (len(res["rows"]), res.get("candidates", 0)), ""]
    for r in res["rows"]:
        ref = r["key"]
        if ref.startswith("c:"):
            ref = "카드 " + ref[2:]
        elif ref.startswith("d:"):
            ref = "문서 " + ref[2:]
        out.append("- [%s] %s · %s · %s · 일치 %s" % (r["kind"], ref, short_project(r["project"]),
                                                       paths.kst_str_z(r["ts"]), r["method"]))
        out.append("  " + r["snippet"])
    return "\n".join(out)


# ---- show --------------------------------------------------------------------
_SHORT_KEY_RX = re.compile(r"^([0-9a-f]{8})/([0-9A-Za-z]{1,8})$")


def _key_range(prefix):
    return prefix, prefix + "\U0010ffff"


def show(con, ref):
    """키 하나의 전문(DB 에 가려 저장된 글만). 키는 search·resume·day 출력에 찍힌 모양 그대로 받는다:
    '카드 <sid>:<uuid>'·'c:…'(카드), '문서 <경로>'·'d:…'(문서), '<sid>:<uuid>'(사건 행, 같은 키의 카드가 있으면 카드),
    '<sid 앞 8자>/<uuid 앞 8자>'(day·resume 짧은 키). 대괄호는 벗긴다. 못 찾으면 None."""
    k = " ".join((ref or "").split()).strip()
    if k.startswith("[") and k.endswith("]"):
        k = k[1:-1].strip()
    if k.startswith("카드 "):
        k = "c:" + k[3:].strip()
    elif k.startswith("문서 "):
        k = "d:" + k[3:].strip()
    if not k:
        return None
    m = _SHORT_KEY_RX.match(k)
    if m:
        lo, hi = _key_range("c:" + m.group(1))
        hits = [r[0] for r in con.execute("SELECT key FROM cards WHERE key >= ? AND key < ?", (lo, hi))
                if r[0].split(":")[-1].startswith(m.group(2))]
        if len(hits) > 1:
            return "\n".join(["# 짧은 키가 카드 %d장에 걸린다: %s" % (len(hits), k)] + ["- 카드 %s" % h[2:] for h in hits])
        if not hits:
            return None
        k = hits[0]
    if k.startswith("d:"):
        return _show_doc(con, k[2:])
    if not k.startswith("c:"):
        if con.execute("SELECT 1 FROM cards WHERE key=?", ("c:" + k,)).fetchone():
            k = "c:" + k
        else:
            out = _show_event(con, k)
            return out if out is not None else _show_doc(con, k)
    return _show_card(con, k)


def _show_card(con, key):
    r = con.execute("SELECT sid, project, start_ts, end_ts, dur_s, human, last_asst, n_tools, files, cmds, n_err,"
                    " human_kind, delivered FROM cards WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    sid, proj, t0, t1, dur, human, last, ntools, files, cmds, nerr, hk, dlv = r
    out = ["# 카드 %s" % key[2:],
           "- 프로젝트 %s · 세션 %s · %s ~ %s · 소요 %s · 도구 %d · 에러 %d · %s" % (
               short_project(proj), sid, paths.kst_str(t0), paths.kst_str_z(t1), fmt_dur(dur), ntools or 0, nerr or 0,
               dict(BUNDLES).get(bundle_of(hk, dlv), "전달 안 된 대기열")),
           "", "## 사람 입력", human or "(없음)", "", "## 마지막 답", last or "(없음)"]
    for title, raw in (("파일", files), ("명령", cmds)):
        try:
            items = json.loads(raw or "[]")
        except ValueError:
            items = []
        if items:
            out += ["", "## %s %d개" % (title, len(items))] + ["- %s" % one_line(x, 300) for x in items]
    return "\n".join(out)


def _show_event(con, key):
    r = con.execute("SELECT e.kind, e.sub, e.ts, e.project, e.sid, e.text, f.kind FROM events e"
                    " LEFT JOIN files f ON f.fid=e.fid WHERE e.key=?", (key,)).fetchone()
    if not r:
        return None
    kind, sub, ts, proj, sid, text, fkind = r
    return "\n".join(["# 행 %s" % key,
                      "- 종류 %s%s · %s · 프로젝트 %s · 세션 %s · 원천 %s" % (
                          kind, ("/" + sub) if sub else "", paths.kst_str_z(ts), short_project(proj), sid or "-",
                          fkind or "-"),
                      "", text or "(본문 없음)"])


def _show_doc(con, rel):
    r = con.execute("SELECT kind, project, updated, text FROM docs WHERE path=?", (rel,)).fetchone()
    if not r:
        return None
    return "\n".join(["# 문서 %s" % rel, "- 종류 %s · 프로젝트 %s · 갱신 %s" % (r[0], short_project(r[1]), paths.kst_str_z(r[2])),
                      "", r[3] or "(본문 없음)"])
