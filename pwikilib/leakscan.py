"""독립 유출 검사. 가림 규칙(redact.PATTERNS)을 쓰지 않는다.

원천 기록(~/.claude 의 줄 파일과 문서)에서 비밀번호·토큰 문맥의 값을 이 모듈만의 규칙으로 뽑고(값은 메모리에만 둔다),
그 값이 출력(DB 파일 바이트·WAL·vault md·로그)에 몇 번 남았는지 센다. 값은 돌려주지 않고 규칙별 개수와 모양(마스크)만 돌려준다.

독립성
- 문맥 규칙을 따로 썼다(줄 원문을 JSON 을 풀지 않고 훑는다. 이스케이프된 따옴표 \\" 를 문맥으로 받는다).
- 값 조건도 따로 둔다: 길이 8 이상, 글자가 있고 숫자나 기호가 있으며, 변수·경로·코드식·상수명이 아닌 것.
  가림의 '강한 값' 조건(글자+숫자, 또는 3종 이상)보다 넓다. 예: 글자+기호만 있는 값도 센다.
- 인코딩 변형은 가림처럼 모양 목록(정규식 갈래)으로 받지 않고, 글을 풀어(normalize) 대조한다:
  \\uXXXX, URL %XX, HTML 엔티티(숫자·이름), 기호 앞 역슬래시를 겹이 없어질 때까지 푼다. 그래서 가림의 모양 목록에 없는
  겹친 인코딩(두 번 URL 인코딩, &amp; 앞머리 등)도 센다. 알려진 값(secrets.local)도 여기서 같이 센다.
"""
import collections
import html
import json
import mmap
import os
import re
import urllib.parse

from . import paths

_LOW = str.maketrans({chr(c): chr(c + 32) for c in range(65, 91)})

# ---- 풀기(정규화, 가림 코드와 따로 쓴다) ----------------------------------------------
_N_UPAIR = re.compile(r"\\+u([dD][89abAB][0-9a-fA-F]{2})\\+u([dD][c-fC-F][0-9a-fA-F]{2})")
_N_U = re.compile(r"\\+u([0-9a-fA-F]{4})")
_N_BS_SYM = re.compile(r"\\+(?=[^A-Za-z0-9\s\\])")   # 기호 앞 역슬래시(셸·JSON 이스케이프)는 지운다
_N_BS_RUN = re.compile(r"\\{2,}")                    # 겹친 역슬래시는 하나로(JSON 두 겹)


def _n_upair(m):
    hi, lo = int(m.group(1), 16), int(m.group(2), 16)
    return chr(0x10000 + ((hi - 0xD800) << 10) + (lo - 0xDC00))


def _n_u(m):
    o = int(m.group(1), 16)
    return m.group(0) if 0xD800 <= o <= 0xDFFF else chr(o)


def normalize(s, rounds=5):
    """인코딩 겹을 푼다: \\uXXXX(역슬래시 몇 개든, 서로게이트 쌍 포함) → HTML 엔티티 → URL %XX → 기호 앞 역슬래시.
    한 바퀴에 한 겹씩, 바뀌지 않을 때까지(최대 rounds) 되풀이한다."""
    for _ in range(rounds):
        t = s
        if "\\" in t:
            t = _N_UPAIR.sub(_n_upair, t)
            t = _N_U.sub(_n_u, t)
        if "&" in t:
            t = html.unescape(t)
        if "%" in t:
            t = urllib.parse.unquote(t, errors="replace")
        if "\\" in t:
            t = _N_BS_RUN.sub(r"\\", _N_BS_SYM.sub("", t))
        if t == s:
            return t
        s = t
    return s

# (이름, 소문자 사본에 쓰는지, 정규식, 줄 사전검사 낱말(바이트, 소문자 사본 기준이면 소문자))
_Q = r"(?:\\{0,3}[\"']|\\{1,4}u00(?:22|27))?"
# 키와 값 사이 구분자: => = : 와 인코딩 모양(\u003d·\u003a, %3D·%3A, &#61;·&#58;·&#x3d;). 소문자 사본용과 원문용을 따로 둔다.
_EQ_LOW = r"(?:=>|=|:|\\{1,4}u003[da]|%3[da]|&#(?:61|58);|&#x3[da];)"
_EQ = r"(?:=>|=|:|\\{1,4}u003[dDaA]|%3[dDaA]|&#(?:61|58);|&#[xX]3[dDaA];)"
_V = r"([^\s\"'\\,;&|<>(){}\[\]`]{6,128})"
RULES = [
    # 키 'pass' 단독은 뺀다: 이 기록에서는 시험 결과 상태(pass: S1_…)·CSS 색 변수(pass: #1ab2c3)·코드(const pass = r?.x)였다.
    ("kv", True, re.compile(r"(?:password|passwd|passphrase|_pass|pwd|_pw|secret(?:_?(?:access_?)?key)?|api_?key"
                            r"|(?:access|auth|refresh)_?token)" + _Q + r"\s{0,4}" + _EQ_LOW + r"\s{0,4}" + _Q + _V),
     (b"pass", b"pwd", b"_pw", b"secret", b"api", b"token")),
    ("env_pw", False, re.compile(r"[A-Z0-9_]{0,40}(?:PW|PASS)" + _Q + r"\s{0,4}" + _EQ + r"\s{0,4}" + _Q + _V),
     (b"PW", b"PASS")),
    ("mysql", False, re.compile(r"(?:mysql|mariadb)[a-z]*(?:[^\n|;&]|\\\\n){0,300}?\s(?:-p|--password=)"
                                r"(?:\\{0,3}['\"])?([^\s'\"\\;|&)]{4,128})"), (b"mysql", b"mariadb")),
    ("identified", True, re.compile(r"identified\s{1,4}(?:with\s{1,4}\S{1,64}\s{1,4})?by\s{1,4}(?:password\s{1,4})?"
                                    r"\\{0,3}['\"]([^'\"\\]{4,128})"), (b"identified",)),
    ("dsn", False, re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,24}://[^\s:/@\"'\\]{1,128}:([^\s@\"'\\/]{4,128})@"), (b"://",)),
    ("sshpass", False, re.compile(r"sshpass\s{1,4}-p\s{0,4}(?:\\{0,3}['\"])?([^\s'\"\\]{4,128})"), (b"sshpass",)),
    ("curl_u", False, re.compile(r"(?:-u|--user)[ =]\s{0,3}(?:\\{0,3}['\"])?[^\s:'\"\\@]{1,64}:([^\s'\"\\@`()]{4,128})"),
     (b"-u", b"--user")),
    ("bearer", True, re.compile(r"bearer\s{1,4}([a-z0-9._~+/=-]{16,512})"), (b"bearer",)),
]


_DIG, _LOW_AZ = "0123456789", "abcdefghijklmnopqrstuvwxyz"
_ALPHA_RUNS = (_DIG + _LOW_AZ, _LOW_AZ + _DIG, _LOW_AZ + _LOW_AZ + _DIG, _DIG + _LOW_AZ[:6] + _LOW_AZ[:6])


def _shape_not_secret(v):
    """(독립 구현) 영숫자가 a·A·9 세 글자뿐인 모양 마스크, 또는 표준 알파벳 문자열의 연속 부분(대소문자 무시)."""
    alnum = set(re.sub(r"[^A-Za-z0-9]", "", v))
    if alnum and not (alnum - set("aA9")):
        return True
    low = v.lower()
    return len(v) >= 8 and any(low in a for a in _ALPHA_RUNS)


def keep_value(v):
    """독립 값 조건(전역 대조 대상 = 흔한 낱말과 겹치기 어려운 값). 길이 8 이상이고, 글자와 숫자가 함께 있거나
    소문자·대문자·숫자·기호 중 3종 이상. 변수·경로·코드식·상수명·자리표시는 뺀다.
    이보다 약한 값(글자+기호만 등)은 이름·식별자와 겹쳐(실측: 한 값이 DB 에 4,578회) 전역 대조하지 않고,
    문맥 자리에 남았는지는 형태 검사(ShapeCounter)가 판정한다."""
    if not v or len(v) < 8 or len(v) > 128:
        return False
    if v[0] in "$%{<([/~.`-*" or "REDAC" in v or "redac" in v:
        return False
    if re.search(r"[(){}\[\]/:\\=]|\?\.", v) or re.search(r"^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]", v):
        return False
    if re.match(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$", v) or re.match(r"^[a-z0-9]+(?:-[a-z0-9]+){3,}$", v):
        return False
    if re.search(r"(?i)x{3,}|\*{3,}|example|your_|changeme|placeholder|dummy", v):
        return False
    if len(set(v)) < 4:
        return False  # 같은 글자 반복 채움 토큰(aaaa…0000)은 비밀이 아니다
    if _shape_not_secret(v):
        return False  # 검사기가 찍은 모양 마스크(Aa9…)·알파벳 열(0~9 다음 a~f 처럼 표준 순서로 이은 글자)은 비밀이 아니다
    kinds = sum(1 for rx in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]") if re.search(rx, v))
    if not (re.search(r"[A-Za-z]", v) and re.search(r"[0-9]", v)) and kinds < 3:
        return False
    return True


def mask(v):
    return re.sub(r"[a-z]", "a", re.sub(r"[A-Z]", "A", re.sub(r"[0-9]", "9", v)))


def _scan_text(text, found):
    """원문과 푼 글(normalize) 둘 다에서 뽑는다(값 집합이라 겹쳐도 한 번). 푼 글은 %253D·&amp;#61;·&quot; 같은
    겹친 인코딩 구분자·따옴표를 맨 모양으로 만든다. 원문도 보는 까닭: 푼 글에서는 값 안의 %2F 같은 조각이 풀려 값이
    달라진다(원문 모양 값도 출력에 그대로 남을 수 있다)."""
    _scan_rules(text, found)
    n = normalize(text)
    if n != text:
        _scan_rules(n, found)


def _scan_rules(text, found):
    low = None
    for name, ci, rx, _ in RULES:
        if ci and low is None:
            low = text.translate(_LOW)
        for m in rx.finditer(low if ci else text):
            s, e = m.span(1)
            v = text[s:e]
            # 셸 이스케이프(\$ \# \!)는 원문 값의 일부가 아니다
            v = re.sub(r"\\([^A-Za-z0-9\s])", r"\1", v)
            if keep_value(v):
                found.setdefault(v, name)


def _line_trigger(raw):
    low = raw.lower()
    for name, ci, rx, pre in RULES:
        t = low if ci else raw
        if any(p in t for p in pre):
            return True
    return False


def extract_source_values(claude=None, limit_bytes=None):
    """원천 전부에서 문맥 값을 뽑는다. {값: 규칙} (메모리에서만)."""
    from .ingest import discover
    claude = claude or paths.claude_dir()
    found = {}
    stats = collections.Counter()
    for rel, kind, proj, sid, how in discover(claude):
        ap = os.path.join(claude, rel)
        try:
            with open(ap, "rb") as fh:
                if how == "jsonl":
                    for raw in fh:
                        stats["lines"] += 1
                        if not _line_trigger(raw):
                            continue
                        stats["trigger_lines"] += 1
                        _scan_text(raw.decode("utf-8", "replace"), found)
                else:
                    stats["docs"] += 1
                    _scan_text(fh.read().decode("utf-8", "replace"), found)
        except OSError:
            continue
    stats["values"] = len(found)
    return found, stats


def _forms(v):
    out = [v.encode("utf-8")]
    j = json.dumps(v, ensure_ascii=False)[1:-1].encode("utf-8")
    if j not in out:
        out.append(j)
    return out


def _char_bytes_rx(ch):
    """값의 글자 하나가 출력 바이트에 적힐 수 있는 모양. ASCII 영숫자는 그대로. 그 밖의 글자는
    (역슬래시 0개 이상 + 글자 UTF-8) 또는 (역슬래시 1개 이상 + uXXXX, 16진 대소문자 무관, BMP 밖은 서로게이트 쌍).
    역슬래시 개수를 열어 두어 셸 이스케이프(\\#)·JSON 한 겹·두 겹을 함께 받는다."""
    b = ch.encode("utf-8")
    if ch.isascii() and ch.isalnum():
        return re.escape(b)
    o = ord(ch)
    units = [o] if o <= 0xFFFF else [0xD800 + ((o - 0x10000) >> 10), 0xDC00 + ((o - 0x10000) & 0x3FF)]
    hexes = []
    for x in units:
        hexes.append(rb"\\+u" + b"".join(("[%s%s]" % (d, d.upper())).encode("ascii") if d.isalpha() else d.encode("ascii")
                                          for d in "%04x" % x))
    return rb"(?:\\*" + re.escape(b) + rb"|" + b"".join(hexes) + rb")"


_ESC_BYTES = (b"%", b"&", b"\\")


class _Probe:
    """값 하나의 대조기. 값에서 가장 긴 ASCII 영숫자 연속(닻)을 파일에서 찾고, 닻 앞뒤 바이트가 값의 나머지 글자와
    맞는지 본다. 닻 위치 하나가 값 한 번이라 겹쳐 세지 않는다.
    맞춤은 두 길이다. (1) 글자별 바이트 모양(평문·역슬래시·\\uXXXX) (2) 앞뒤 바이트를 풀어(normalize) 값 조각과 견줌.
    (2)는 앞뒤에 % & \\ 가 있을 때만 한다(풀 것이 없으면 (1)과 같다). 겹친 인코딩(%2523, &amp;amp;, &amp;#35;)은 (2)가 잡는다."""
    __slots__ = ("anchor", "pre_rx", "suf_rx", "pre_span", "suf_span", "pre_n", "suf_n")

    def __init__(self, v):
        runs = [(m.end() - m.start(), -m.start(), m.start(), m.group(0)) for m in re.finditer(r"[A-Za-z0-9]+", v)]
        if not runs:
            raise ValueError("영숫자 없는 값")
        _, _, a, anc = max(runs)
        pre, suf = v[:a], v[a + len(anc):]
        self.anchor = anc.encode("ascii")
        self.pre_rx = re.compile(b"".join(_char_bytes_rx(c) for c in pre) + rb"\Z") if pre else None
        self.suf_rx = re.compile(b"".join(_char_bytes_rx(c) for c in suf)) if suf else None
        # 한 글자가 가장 길게 적히는 모양(역슬래시 여러 개 + \uXXXX, &amp;amp;, &amp;#x…;)을 덮는 폭
        self.pre_span = 24 * len(pre) + 24
        self.suf_span = 24 * len(suf) + 24
        self.pre_n = normalize(pre)
        self.suf_n = normalize(suf)

    def _norm_ok(self, pw, sw):
        if self.pre_n and not normalize(pw.decode("utf-8", "replace")).endswith(self.pre_n):
            return False
        if self.suf_n and not normalize(sw.decode("utf-8", "replace")).startswith(self.suf_n):
            return False
        return True

    def count(self, mm):
        n = 0
        L = len(self.anchor)
        p = mm.find(self.anchor)
        while p >= 0:
            pw = mm[max(0, p - self.pre_span):p] if self.pre_rx is not None else b""
            sw = mm[p + L:p + L + self.suf_span] if self.suf_rx is not None else b""
            ok = (self.pre_rx is None or self.pre_rx.search(pw) is not None) and \
                 (self.suf_rx is None or self.suf_rx.match(sw) is not None)
            if not ok and any(e in pw or e in sw for e in _ESC_BYTES):
                ok = self._norm_ok(pw, sw)
            if ok:
                n += 1
            p = mm.find(self.anchor, p + 1)
        return n


def count_in_bytes(data, values, probes=None):
    """바이트열(또는 str 을 UTF-8 로)에서 값마다 몇 번 나오는지. count_in_file 과 같은 모양을 센다. {값: 개수}"""
    if isinstance(data, str):
        data = data.encode("utf-8", "surrogatepass")
    out = {}
    if not data:
        return out
    probes = probes if probes is not None else make_probes(values)
    for v in values:
        pr = probes.get(v)
        if pr is None:
            n = sum(data.count(f) for f in _forms(v))
        else:
            n = pr.count(data)
        if n:
            out[v] = n
    return out


def count_in_file(path, values, probes=None):
    """파일 바이트에서 값마다 몇 번 나오는지. {값: 개수}
    모양: 평문, 특수기호 앞 역슬래시(셸·JSON 한 겹·두 겹), 특수기호·비ASCII 의 \\uXXXX 이스케이프(일부 글자만 바꾼 것 포함)."""
    out = {}
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return out
    probes = probes if probes is not None else make_probes(values)
    with open(path, "rb") as fh:
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            for v in values:
                pr = probes.get(v)
                if pr is None:
                    n = 0
                    for f in _forms(v):
                        p = mm.find(f)
                        while p >= 0:
                            n += 1
                            p = mm.find(f, p + 1)
                else:
                    n = pr.count(mm)
                if n:
                    out[v] = n
        finally:
            mm.close()
    return out


def make_probes(values):
    out = {}
    for v in values:
        try:
            out[v] = _Probe(v)
        except ValueError:
            pass  # 영숫자 없는 값은 평문·JSON 모양만 찾는다
    return out


def scan(db_path=None, vault=None, extra_dirs=None, harvested=None, known=None):
    """독립 판정. 원천에서 뽑은 값 + 수확값(강한 값) + 알려진 값(secrets.local) 이 출력에 남은 개수.
    값 대신 모양만 돌려준다. 알려진 값은 모양(마스크)도 내지 않고 'known' 으로만 묶는다.
    알려진 값도 여기서 센다: 구현 검사(redact.Checker)는 가림과 같은 변형 정규식을 써서 가림이 놓친 모양을 함께 놓친다."""
    found, stats = extract_source_values()
    known = set(known or [])
    vals = {v: ("known" if v in known else r) for v, r in found.items()}
    for v in known:
        vals[v] = "known"
    for v in harvested or []:
        vals.setdefault(v, "harvested")
    db_path = db_path or paths.db_path()
    files = [("db_file", db_path), ("wal", db_path + "-wal")]
    vault = vault or paths.vault_dir()
    if os.path.isdir(vault):
        for dp, dn, fn in os.walk(vault):
            for f in fn:
                if f.endswith(".md"):
                    files.append(("vault_md", os.path.join(dp, f)))
    for d in extra_dirs or (paths.logs_dir(), paths.runs_dir()):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                p = os.path.join(d, f)
                if os.path.isfile(p):
                    files.append(("logs_runs", p))
    hits = collections.Counter()
    per_val = collections.Counter()
    probes = make_probes(list(vals))
    for loc, p in files:
        for v, n in count_in_file(p, list(vals), probes).items():
            hits[loc] += n
            per_val[v] += n
    shapes = collections.Counter()
    for v, n in per_val.items():
        shapes["known(모양 숨김)" if vals[v] == "known" else "%s %s" % (vals[v], mask(v))] += n
    rules = collections.Counter(vals.values())
    return {"source": dict(stats), "values": len(vals), "values_by_rule": dict(rules),
            "files": len(files), "hits": dict(hits), "values_hit": len(per_val),
            "shapes_hit": dict(shapes.most_common(40))}


# ---- 형태 검사(독립) -------------------------------------------------------------
# 문맥 규칙(RULES)이 잡은 자리에 가림 토큰이 아닌 리터럴이 남았는지 센다. 값의 강도와 무관하다(약한 값도 센다).
NOT_VALUES = {"true", "false", "null", "none", "nil", "str", "string", "int", "bool", "boolean", "required", "optional",
              "undefined", "number", "object", "any", "unknown", "void", "password", "secret", "token", "string;",
              "str,", "input", "hidden", "text", "prompt", "masked", "redacted", "empty", "missing", "notset", "unset"}


def literal_value(v, rule=None):
    if not v or v.startswith("[REDAC") or v.startswith("[redac"):
        return False
    if rule == "curl_u" and v.isdigit():
        return False  # -u 1000:1000 은 uid:gid
    if v[0] in "$%{<([/~.`*-\u2026" or v.lower() in NOT_VALUES:
        return False  # '…' 로 시작하면 출력이 자르며 붙인 표시(…(잘림))다. 값이 잘려 나간 자리이지 값이 아니다
    if re.match(r"^(?:os|self|env|process|config|settings|args|req|request|opts|options|cfg|conf|params)\.", v):
        return False
    return True


class ShapeCounter:
    def __init__(self):
        self.c = collections.Counter()
        self.masks = collections.Counter()

    def text(self, text):
        """원문과 푼 글(normalize)을 각각 세어 규칙마다 큰 쪽을 더한다(같은 자리를 두 번 세지 않는다).
        푼 글은 %253D·&amp;#61;·=&quot;…&quot;·\\u0020= 처럼 겹친 인코딩 문맥을 맨 모양으로 만든다."""
        if not text:
            return
        c0, m0 = self._count(text)
        n = normalize(text)
        if n != text:
            c1, m1 = self._count(n)
            for k in set(c0) | set(c1):
                if c1[k] > c0[k]:
                    c0[k] = c1[k]
                    m0[k] = m1[k]
        for k, v in c0.items():
            if v:
                self.c[k] += v
        for k, ms in m0.items():
            for mk in ms:
                if len(self.masks) < 200 or mk in self.masks:
                    self.masks[mk] += 1

    def _count(self, text):
        c = collections.Counter()
        ms = collections.defaultdict(list)
        low = None
        for name, ci, rx, pre in RULES:
            if ci:
                if low is None:
                    low = text.translate(_LOW)
                t = low
            else:
                t = text
            if not any(p.decode("utf-8") in t for p in pre):
                continue
            for m in rx.finditer(t):
                s, e = m.span(1)
                v = text[s:e]
                # 가림 토큰은 값 그룹 문자 집합 밖([ 로 시작)이라 값이 토큰 앞에서 끊긴다: 바로 뒤가 토큰이면 리터럴이 아니다
                if text.startswith("[REDAC", e) or not literal_value(v, name):
                    continue
                c[name] += 1
                ms[name].append("%s %s" % (name, mask(v)))
        return c, ms
