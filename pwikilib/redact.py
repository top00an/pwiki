"""Redaction. Applied to JSON-decoded strings before storage, FTS and md export.

Stage 1 (values): replaces known values. Known values = secrets.local (entered by a person) + harvested values (caught in password
contexts during collection).
  - Whole value: escape, URL-encoding, HTML-entity and line-split variants included.
  - Long fragments of a value: contiguous substrings of length max(8, ceil(0.7×length)) or more that contain at least one digit or
    special character (values written in the source with the last characters missing, or only the alphanumeric core written
    separately). Letter-only fragments (account names) are not replaced.
  - Value hashes: when the first 6+ hex chars of sha256, sha1 or md5 (of value, value+newline) appear as a standalone token, they are replaced.
Stage 2 (shapes): regexes (DSN user:pass@, mysql -p, sshpass -p, curl -u, PASSWORD=, *_PW=, *PASS= family, IDENTIFIED BY, Bearer,
  sk-ant-, gh*_, AKIA, xox*-, AIza, apikey_<hex>, PRIVATE KEY, JWT, mobile phone, email). Private IPs are not redacted
  (working knowledge). The leading boundary also treats the position right after \\n, \\t, \\r of re-serialized JSON as a boundary.

Harvest: collects values caught by stage-2 rules that have a value group (password and token contexts). Strong values go into the
stage-1 value list as plaintext (replaced even when the same value appears in another context); weak values (may be common words)
are added as hashes only. Values live only in ~/.pwiki/secrets.harvested (600).

Known values are never written to code, logs or md. Checkers count only.
"""
import hashlib
import math
import os
import re
import stat

TOKEN = "[REDACTED:%s]"
_NOT_TOKEN = r"(?!\[(?:REDAC|redac))"  # 소문자 사본에서는 토큰이 [redacted: 로 보인다


def _after(lit, cls):
    """리터럴 바로 뒤에 두는 앞 경계 검사: 리터럴 앞 글자가 cls 가 아니거나, 다시 직렬화된 JSON 의 \\n \\t \\r 이다.
    경계를 패턴 맨 앞에 두면 모든 위치에서 되돌아보기를 해 느리므로(실측 4~7배) 리터럴을 먼저 찾고 뒤에서 확인한다.
    lit 는 고정 길이 정규식 조각이어야 한다."""
    return r"(?:(?<![%s]%s)|(?<=\\[ntr]%s))" % (cls, lit, lit)


# 따옴표: 맨 따옴표(역슬래시 0~3개 앞) 또는 \uXXXX 로 적힌 따옴표(Gson 은 ' 를 ' 로 적는다).
_Q = r"(?:\\{0,3}[\"']|\\{1,4}u00(?:22|27)|(?:&amp;|&)(?:quot|apos|#3[49]|#[xX]2[27]);)?"
# 위 셋째 갈래: HTML 엔티티 따옴표(&quot; &apos; &#34; &#39; &#x22; &#x27;, 한 겹 더 인코딩한 &amp; 앞머리 포함).
# 키·구분자·값 사이 공백: 맨 공백,  (JSON 인코더), %20(URL), &#32;·&nbsp;(HTML)
_SP = r"(?:\s|\\{1,4}u0020|%20|&#32;|&nbsp;)"
_KV_NOT_VALUE = (
    r"(?![$%{<(\[\\/])"
    r"(?!(?:true|false|null|none|nil|str|string|int|bool|required|optional|undefined"
    r"|os\.|self\.|env\.|process\.|getenv|input)(?![A-Za-z0-9_]))"
)
# 따옴표 안 값(공백 허용) 또는 맨 값. 치환은 값 그룹(v1·v2·v3) 구간만 한다.
_KV_VALUE = (
    r"(?:(?<=\")" + _NOT_TOKEN + _KV_NOT_VALUE + r"(?P<v1>[^\"\\\n]{1,200}?)(?=\\{0,3}\")"
    r"|(?<=')" + _NOT_TOKEN + _KV_NOT_VALUE + r"(?P<v2>[^'\\\n]{1,200}?)(?=\\{0,3}')"
    r"|" + _NOT_TOKEN + _KV_NOT_VALUE + r"(?P<v3>(?:[^\s\"',;&)<>`\]}\\]|\\(?![ntr\"\\/u])"
    r"|\\{1,4}u(?!00(?:2[0267]|3[cCeE]))[0-9a-fA-F]{4}){3,200}))"
)
# 키와 값 사이: = : 와 그 인코딩 모양(=·:(Gson 등 JSON 인코더), %3D·%3A(URL), &#61;·&#58;·&#x3d;(HTML)).
# 값 안의 \uXXXX(공백·따옴표·& < > 제외)는 값의 일부로 본다(예: base64 끝 == 가 == 로 적힘).
# 더한 모양: => (PHP·Ruby·Perl 키값), %253D(URL 두 겹), &amp;#61;(HTML 두 겹).
_KV_EQ = r"(?:=>|[:=]|\\{1,4}u003[dDaA]|(?:%25|%)3[dDaA]|(?:&amp;|&)#(?:61|58);|(?:&amp;|&)#[xX]3[dDaA];)"
_KV_SEP = _Q + _SP + "{0,3}" + _KV_EQ + _SP + "{0,3}" + _Q
# 대소문자 무시 낱말(소문자 사본에서 찾는다): *password·*passwd·*pwd(PGPASSWORD·dbPassword·MYSQL_PWD),
# *_pass·*_pw(OWNER_PW·DB_PASS), *secret·*secret_key·*secret_access_key, api_key, *_token 류.
# 낱말 앞 식별자(PG·OWNER_ 등)는 치환 대상이 아니므로 일치에 넣지 않는다.
_KV_WORDS = (
    r"password|passwd|passphrase|pwd|_pass|_pw"
    r"|비밀번호|패스워드"
    r"|client[_-]?secret|secret[_-]?(?:access[_-]?)?key|secret"
    r"|api[_-]?key|access[_-]?token|auth[_-]?token|refresh[_-]?token"
)
# 대소문자 구분 낱말: 환경변수형 ROOTPW·PGPASS·PW(앞이 소문자가 아님), 낙타형 rootPw·dbPass(앞이 소문자·숫자)
_ENV_WORDS = r"PW(?<![a-z]PW)|PASS(?<![a-z]PASS)|Pw(?<=[a-z0-9]Pw)|Pass(?<=[a-z0-9]Pass)"
_CLI_VALUE = (r"(?:'" + _NOT_TOKEN + r"(?P<v1>[^'\n]{1,200})'|\"" + _NOT_TOKEN + r"(?P<v2>[^\"\n]{1,200})\"|"
              + _NOT_TOKEN + r"(?![$\\])(?P<v3>(?:[^\s'\"`;|&)\\]|\\(?![ntr\"\\/u])){1,200}))")
_MYSQL_BIN = r"(?:mysql|mariadb|mysqldump|mysqladmin|mysqlsh|mysqlimport|mysqlcheck|mariadb-dump)"
EMAIL_KEEP_LOCALPARTS = ["root", "ubuntu", "ec2-user", "git", "centos", "debian", "pi", "opc", "azureuser", "vagrant",
                         "lima"]
_PHONE_TAIL = r"[-. ]?[0-9]{3,4}[-. ]?[0-9]{4}(?![0-9A-Za-z\-_])"
_PHONE_B = r"0-9A-Za-z.+\-_\\"

# (종류, 패턴, 사전검사 부분문자열 목록 또는 None, 치환 방식, 소문자 사본 여부)
# 치환 방식: "all" 은 일치 전체, "v" 는 값 그룹 구간만. 소문자 사본 규칙은 ASCII 만 소문자로 바꾼 사본(길이 같음)에서 찾고
# 원문의 같은 구간을 바꾼다(대소문자 무시 정규식은 리터럴 최적화가 꺼져 실측 3배 느리다). 사전검사도 소문자 사본에 한다.
PATTERNS = [
    ("private_key",
     r"-----BEGIN ((?:[A-Z0-9]+ )*)PRIVATE KEY-----"
     r"(?:(?:(?!-----END )[\s\S]){0,20000}?-----END \1PRIVATE KEY-----|(?:[A-Za-z0-9+/=\s]|\\n|\\r){0,20000})",
     ["PRIVATE KEY"], "all", False),
    ("jwt", r"eyJ" + _after("eyJ", "A-Za-z0-9_-") + r"[A-Za-z0-9_-]{10,2000}\.[A-Za-z0-9_-]{10,8000}\.[A-Za-z0-9_-]{10,2000}",
     ["eyJ"], "all", False),
    ("anthropic_key", r"sk-ant-" + _after("sk-ant-", "A-Za-z0-9_-") + r"[A-Za-z0-9_-]{10,300}", ["sk-ant-"], "all", False),
    ("sk_key", r"sk-" + _after("sk-", "A-Za-z0-9_-") + r"(?!ant-)(?:proj-)?[A-Za-z0-9_-]{20,300}", ["sk-"], "all", False),
    ("github_token", r"(?:gh[pousr]_" + _after("gh._", "A-Za-z0-9_") + r"[A-Za-z0-9]{20,255}|github_pat_"
     + _after("github_pat_", "A-Za-z0-9_") + r"[A-Za-z0-9_]{20,255})",
     ["gh", "github_pat_"], "all", False),
    ("aws_key", r"(?:AKIA|ASIA)" + _after("....", "A-Za-z0-9") + r"[0-9A-Z]{16}(?![A-Za-z0-9])", ["AKIA", "ASIA"], "all",
     False),
    ("slack_token", r"xox[abposr]-" + _after("xox..", "A-Za-z0-9") + r"[A-Za-z0-9-]{10,200}", ["xox"], "all", False),
    ("google_key", r"AIza" + _after("AIza", "A-Za-z0-9_-") + r"[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])", ["AIza"], "all",
     False),
    # 뒤 토막은 영숫자를 끝까지 먹는다(16진이 아닌 글자가 붙으면 되짚기로 앞 토막만 가리던 결함).
    ("apikey_hex", r"apikey_" + _after("apikey_", "A-Za-z0-9") + r"[0-9a-fA-F]{8,128}(?:_[0-9A-Za-z]{8,128})*"
     r"(?![0-9A-Za-z])", ["apikey_"], "all", False),
    ("bearer", r"bearer" + _after("bearer", "a-z0-9") + r"\s{1,4}" + _NOT_TOKEN + r"(?P<v1>[A-Za-z0-9._~+/=-]{16,4096})",
     ["bearer"], "v", True),
    ("dsn", r"(?:(?<![A-Za-z0-9+.-])|(?<=\\[ntr]))[A-Za-z][A-Za-z0-9+.-]{1,24}://[^\s:/@\"'<>]{1,256}:" + _NOT_TOKEN
     + r"(?![$%{<\\])(?P<v1>[^\s@\"'<>/]{1,256})(?=@)", ["://"], "v", False),
    ("mysql_p", r"(?:(?<![A-Za-z0-9_-])|(?<=\\[ntr]))" + _MYSQL_BIN
     + r"(?![A-Za-z0-9_-])(?:[^\n|;&]|\\\n){0,300}?\s(?:-p|--password=)" + _CLI_VALUE, ["mysql", "mariadb"], "v", False),
    ("sshpass", r"sshpass" + _after("sshpass", "A-Za-z0-9_-") + r"\s{1,4}-p\s{0,4}" + _CLI_VALUE, ["sshpass"], "v",
     False),
    ("basic_auth", r"(?:(?<![A-Za-z0-9_-])|(?<=\\[ntr]))(?:curl|wget|https?|httpie)(?![A-Za-z0-9_-])[^\n|;&]{0,300}?\s"
     r"(?:-u|--user|--http-user)[ =]?\s{0,3}[\"']?[^\s:\"'@]{1,64}:" + _NOT_TOKEN
     + r"(?![$\\])(?P<v1>[^\s\"'@;|&]{3,200})", ["-u", "--user", "--http-user"], "v", False),
    ("identified_by", r"identified" + _after("identified", "a-z0-9") + r"\s{1,4}(?:with\s{1,4}\S{1,64}\s{1,4})?by\s{1,4}"
     r"(?:password\s{1,4})?(?P<q>\\{0,3}['\"])" + _NOT_TOKEN + r"(?P<v1>[^'\"\\\n]{1,200})(?P=q)",
     ["identified"], "v", True),
    ("pw_kv", r"(?:" + _KV_WORDS + r")" + _KV_SEP + _KV_VALUE,
     ["pass", "pwd", "_pw", "비밀번호", "패스워드", "secret", "api_key", "apikey", "api-key", "_token", "-token"], "v", True),
    ("pw_env", r"(?:" + _ENV_WORDS + r")" + _KV_SEP + _KV_VALUE, ["PW", "PASS", "Pw", "Pass"], "v", False),
    # 휴대전화. 앞뒤가 영숫자·하이픈·점이면(uuid·해시·소수) 전화번호로 보지 않는다. 글자 바로 뒤(Tel010-…)는 구분자가 있을 때만.
    ("phone", r"(?:01[016789]" + _after("01.", _PHONE_B) + r"|\+82" + _after(r"\+82", _PHONE_B) + r"[-. ]?1[016789])"
     + _PHONE_TAIL + r"|01[016789](?<=[A-Za-z]01.)[-. ][0-9]{3,4}[-. ][0-9]{4}(?![0-9A-Za-z\-_])",
     ["01", "+82"], "all", False),
    # ssh·scp 대상(root@host 등 흔한 유닉스 계정)은 이메일이 아니라 서버 주소라 남긴다. admin@ 은 로그인 계정일 수 있어 가린다.
    ("email", r"(?:(?<![A-Za-z0-9._%+\-\\])|(?<=\\[ntr]))(?!(?:" + "|".join(EMAIL_KEEP_LOCALPARTS) + r")@)"
     r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
     r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?){0,8}\.[A-Za-z]{2,24}(?![A-Za-z0-9-])",
     ["@"], "all", False),
]
HARVEST_KINDS = {"bearer", "dsn", "mysql_p", "sshpass", "basic_auth", "identified_by", "pw_kv", "pw_env"}
# apikey_<16진>_<16진> 토큰은 16진 부분(12자 이상)을 따로 수확한다. 앞부분만 자리표시로 바꾸고 뒷부분을 그대로 적거나
# 16진 부분만 따로 인용한 글이 있었다(실측: 새 DB 에 16자 토막 13회·39자 토막 13회).
_APIKEY_PARTS = re.compile(r"apikey_([0-9a-fA-F]{8,128}(?:_[0-9A-Za-z]{8,128})*)")
_ASCII_LOWER = str.maketrans({chr(c): chr(c + 32) for c in range(65, 91)})


def ascii_lower(s):
    return s.translate(_ASCII_LOWER)


KINDS = ["known", "known_part", "known_hash"] + [p[0] for p in PATTERNS]

_SEP = r"(?:\\?\r?\n[ \t]*|\\n)?"
_ID_RX = re.compile(r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
                    r"|(?:msg|toolu|srvtoolu|req)_[A-Za-z0-9]{8,40})$")
_HEX_RX = re.compile(r"(?:(?<![0-9A-Za-z])|(?<=\\[ntr]))[0-9a-f]{6,128}(?![0-9A-Za-z])")


def _u_escape_rx(o):
    """\\uXXXX 이스케이프(JSON·JS·Go 인코더가 특수기호·비ASCII 를 적는 모양) 정규식. 16진은 대소문자 모두, BMP 밖 글자는
    서로게이트 쌍. 앞 역슬래시 개수는 부르는 쪽의 \\\\{0,8} 이 받는다."""
    if o > 0xFFFF:
        v = o - 0x10000
        units = [0xD800 + (v >> 10), 0xDC00 + (v & 0x3FF)]
    else:
        units = [o]
    return r"\\{0,8}".join(r"\\u" + "".join("[%s%s]" % (d, d.upper()) if d.isalpha() else d for d in "%04x" % u)
                           for u in units)


_HTML_NAMED = {"&": "amp", '"': "quot", "'": "apos", "<": "lt", ">": "gt"}


def _char_alts(ch):
    """글자 하나가 적힐 수 있는 모양. 한 겹: URL %XX, HTML 숫자·16진·이름 엔티티, \\uXXXX.
    두 겹: URL %25XX, HTML &amp;#…; ·&amp;이름; (인코딩을 한 번 더 한 글. 예: 질의 문자열 안 URL, HTML 안 HTML)."""
    o = ord(ch)
    alts = [re.escape(ch), "%%%02X" % o, "%%%02x" % o, "&#%d;" % o, "&#x%x;" % o, "&#x%X;" % o, _u_escape_rx(o),
            "%%25%02X" % o, "%%25%02x" % o, "&amp;#%d;" % o, "&amp;#x%x;" % o, "&amp;#x%X;" % o]
    nm = _HTML_NAMED.get(ch)
    if nm:
        alts += ["&%s;" % nm, "&amp;%s;" % nm]
    seen = []
    for a in alts:
        if a not in seen:
            seen.append(a)
    return seen


def variant_pattern(value):
    """알려진 값 하나를 변형까지 잡는 정규식 문자열로 만든다.
    특수문자 앞 역슬래시(셸·JSON 이스케이프), URL 인코딩, HTML 엔티티, \\uXXXX 이스케이프, 특수문자 1~2회 반복,
    글자 사이 줄바꿈(줄 분할)을 허용한다."""
    parts = []
    for ch in value:
        if ch.isalnum() and ord(ch) < 128:
            parts.append(re.escape(ch))
        else:
            parts.append(r"(?:\\{0,8}(?:" + "|".join(_char_alts(ch)) + r")){1,2}")
    return _SEP.join(parts)


def windows(value, frac=0.7, min_len=8):
    """값의 긴 연속 조각. 길이 max(min_len, ceil(frac×길이)) 이상, 값 전체보다 짧고, 숫자·특수문자가 하나 이상 든 것.
    긴 것부터."""
    L = len(value)
    k = max(min_len, int(math.ceil(frac * L)))
    out = []
    for n in range(L - 1, k - 1, -1):
        for i in range(0, L - n + 1):
            w = value[i:i + n]
            if re.search(r"[^A-Za-z]", w) and w not in out:
                out.append(w)
    return out


def _fragments(value):
    """사전검사용 3글자 영숫자 조각(영숫자 연속 구간마다 3글자 창 전부). 줄 분할·변형이 한 조각을 깨도 나머지가 남는다."""
    frags = []
    for r in re.findall(r"[A-Za-z0-9]{3,}", value):
        for i in range(0, len(r) - 2):
            f = r[i:i + 3]
            if f not in frags:
                frags.append(f)
    return frags


def digests(value):
    out = []
    for b in (value.encode("utf-8"), (value + "\n").encode("utf-8")):
        out += [hashlib.sha256(b).hexdigest(), hashlib.sha1(b).hexdigest(), hashlib.md5(b).hexdigest()]
    return out


def hash_prefixes(digest_list, min_len=6):
    s = set()
    for d in digest_list:
        for k in range(min_len, len(d) + 1):
            s.add(d[:k])
    return s


# ---- 수확값 강도 ------------------------------------------------------------------
# 'test'·'sample' 로 시작하는 값도 버리지 않는다(진짜 값일 수 있다. 실측: 버린 bearer 토큰 2개가 다른 문맥에 43회 남음).
_PLACEHOLDER = re.compile(r"(?i)x{3,}|\*{3,}|\.{3,}|example|your[_-]?|changeme|placeholder|dummy|redac|<|>")
_CODE_LIKE = re.compile(r"[(){}\[\]/:\\]|=.|\?\.|^[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]")
_CONST_LIKE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$|^[a-z0-9]+(?:-[a-z0-9]+){3,}$")


_U_ESC = re.compile(r"\\+u([0-9a-fA-F]{4})")


def _u_unescape(s):
    """\\uXXXX(역슬래시 1개 이상)를 글자로 푼다. 서로게이트(D800~DFFF)는 풀지 않는다."""
    def f(m):
        o = int(m.group(1), 16)
        return m.group(0) if 0xD800 <= o <= 0xDFFF else chr(o)
    return _U_ESC.sub(f, s)


def candidate_forms(v):
    """수확한 값의 원래 모양들. 셸 이스케이프(\\$ \\# \\!)를 풀고, URL 인코딩(%XX)이 있으면 푼 모양도,
    \\uXXXX 가 있으면 푼 모양도 더한다."""
    import urllib.parse
    v = (v or "").strip()
    out = []
    u = re.sub(r"\\([^A-Za-z0-9\s])", r"\1", v)
    if _U_ESC.search(v):
        u = _u_unescape(v)
        u = re.sub(r"\\([^A-Za-z0-9\s])", r"\1", u)
    for x in (u, urllib.parse.unquote(u) if re.search(r"%[0-9A-Fa-f]{2}", u) else None):
        if x and x not in out:
            out.append(x)
    return out


# 비밀이 아닌 모양(수확 오탐). 1) 모양 마스크: 영숫자가 a·A·9 뿐인 값(검사기가 값 대신 찍는 마스크, 예: Aa9Aaaa#Aaa).
# 2) 알파벳 열: 표준 알파벳 문자열의 연속 부분(예: 0~9 다음 a~f 를 이은 16진 알파벳).
_D, _L, _U = "0123456789", "abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnopqrstuvwxyz".upper()
_ALPHABETS = (_D + _L, _D + _U, _L + _D, _U + _D, _L + _U + _D, _U + _L + _D, _D + _L[:6] + _U[:6])


def not_secret_shape(v):
    al = {c for c in v if c.isascii() and c.isalnum()}
    if al and al <= {"a", "A", "9"}:
        return True
    return len(v) >= 8 and any(v in a for a in _ALPHABETS)


def value_strength(v):
    """수확한 값의 강도. None(버림: 변수·경로·자리표시·코드식·상수명), 'weak'(해시만 올림), 'strong'(평문으로 올림).
    강함: 길이 8 이상이고 (글자와 숫자가 함께 있거나, 소문자·대문자·숫자·기호 중 3종 이상)."""
    if not v or len(v) < 4 or len(v) > 200 or re.search(r"\s", v):
        return None
    if v[0] in "$%{<([\\/~.`-" or _PLACEHOLDER.search(v) or _CODE_LIKE.search(v) or _CONST_LIKE.search(v):
        return None
    if len(set(v)) <= 2 or not_secret_shape(v):
        return None
    classes = sum(1 for rx in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[^A-Za-z0-9]") if re.search(rx, v))
    has_letter = re.search(r"[A-Za-z]", v) is not None
    has_digit = re.search(r"[0-9]", v) is not None
    if len(v) >= 8 and ((has_letter and has_digit) or classes >= 3):
        return "strong"
    return "weak"


# ---- 값 목록 파일 ------------------------------------------------------------------
def _read_lines_600(path, warn):
    if not os.path.exists(path):
        return []
    st = os.stat(path)
    if warn is not None and (st.st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
        warn.append("%s 권한이 600 보다 넓다(%o)" % (os.path.basename(path), st.st_mode & 0o777))
    with open(path, "r", encoding="utf-8") as fh:
        return [ln.rstrip("\n").rstrip("\r") for ln in fh]


def load_known_values(path=None, warn=None):
    """secrets.local 에서 알려진 값을 읽는다. 한 줄에 하나, # 주석, 6자 미만은 무시한다.
    파일 권한이 600 보다 넓으면 warn 에 알린다(값은 쓰지 않는다)."""
    from . import paths
    vals = []
    for v in _read_lines_600(path or paths.secrets_path(), warn):
        if not v or v.lstrip().startswith("#") or len(v) < 6:
            continue
        if v not in vals:
            vals.append(v)
    return vals


class Harvested:
    """수확값 저장소(~/.pwiki/secrets.harvested, 600). 줄 형식: 'v<TAB>값'(강한 값), 'h<TAB>해시…'(약한 값의 해시)."""

    def __init__(self, path=None):
        from . import paths
        self.path = path or paths.harvest_path()
        self.strong = []
        self.weak = []  # 해시 목록의 목록
        self._weak_keys = set()
        self.dirty = False
        # 이번 실행에서 새로 올라간 값(소급 가림 대상). 메모리에만 둔다.
        self.new_strong = []
        self.new_weak = []

    def active_strong(self):
        """가림·검사에 쓰는 강한 값. 파일에는 있지만 지금 규칙으로 비밀이 아닌 모양(마스크·알파벳 열)인 값은 뺀다.
        파일 내용은 바꾸지 않는다(저장 때도 그대로 둔다)."""
        return [v for v in self.strong if value_strength(v) == "strong"]

    def load(self, warn=None):
        for ln in _read_lines_600(self.path, warn):
            if ln.startswith("v\t"):
                v = ln[2:]
                if v and v not in self.strong:
                    self.strong.append(v)
            elif ln.startswith("h\t"):
                ds = ln[2:].split("\t")
                if ds and ds[0] not in self._weak_keys:
                    self._weak_keys.add(ds[0])
                    self.weak.append(ds)
        return self

    def add_raw(self, value, exclude=()):
        """수확한 원래 문자열 하나를 원래 모양들로 풀어 올린다. 올라간 강도 목록을 돌려준다."""
        return [r for r in (self.add(f, exclude) for f in candidate_forms(value)) if r]

    def add(self, value, exclude=()):
        s = value_strength(value)
        if s is None or value in exclude:
            return None
        if s == "strong":
            if value not in self.strong:
                self.strong.append(value)
                self.new_strong.append(value)
                self.dirty = True
                return "strong"
            return None
        ds = digests(value)
        if ds[0] not in self._weak_keys:
            self._weak_keys.add(ds[0])
            self.weak.append(ds)
            self.new_weak.append(ds)
            self.dirty = True
            return "weak"
        return None

    def save(self):
        if not self.dirty:
            return
        d = os.path.dirname(self.path)
        os.makedirs(d, mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# pwiki 수확값(비밀번호·토큰 문맥에서 잡은 값). 권한 600. 값은 코드·로그·md 에 쓰지 않는다.\n")
            for v in self.strong:
                fh.write("v\t%s\n" % v)
            for ds in self.weak:
                fh.write("h\t%s\n" % "\t".join(ds))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self.dirty = False


def _splice(m, tok, s):
    """값 그룹이 있으면 그 구간만, 없으면 일치 전체를 토큰으로. 구간은 원문 s 기준(소문자 사본과 길이가 같다)."""
    gd = m.groupdict()
    b, e0 = m.span()
    for nm in ("v1", "v2", "v3"):
        if gd.get(nm) is not None:
            vs, ve = m.span(nm)
            return s[b:vs] + tok + s[ve:e0]
    return tok


def _values(m, s):
    gd = m.groupdict()
    for nm in ("v1", "v2", "v3"):
        if gd.get(nm) is not None:
            vs, ve = m.span(nm)
            return s[vs:ve]
    return None


def _sub(rx, ci, s, tok, mode):
    target = ascii_lower(s) if ci else s
    out = []
    last = 0
    n = 0
    for m in rx.finditer(target):
        out.append(s[last:m.start()])
        out.append(tok if mode == "all" else _splice(m, tok, s))
        last = m.end()
        n += 1
    if not n:
        return s, 0
    out.append(s[last:])
    return "".join(out), n


def _prefilter_ok(pre, ci, s, sl):
    if pre is None:
        return True
    t = sl if ci else s
    return any(p in t for p in pre)


def _anchors(value, frac=0.7, min_len=8):
    """사전검사용 닻. 값 전체와 긴 조각(길이 k 이상) 모두에 드는 가운데 구간 [L-k, k) 안에서 가장 긴 영숫자 연속을 고르고,
    6글자 이상이면 앞·뒤 반쪽(각 5글자 이상, 짧으면 가운데가 겹침)으로 나눈다(줄 분할은 대개 한쪽만 깬다). 영숫자는 이스케이프·URL 인코딩·
    HTML 엔티티 변형에서도 그대로 남는다. 3·4글자 조각은 흔해서(실측: 닻이 든 문자열 42,332개 중 실제 일치 186개) 쓰지 않는다.
    가운데 구간에 3글자 이상 영숫자 연속이 없으면 값의 영숫자 조각 전부를 쓴다."""
    L = len(value)
    k = max(min_len, int(math.ceil(frac * L)))
    lo, hi = (L - k, k) if k < L else (0, L)
    runs = [(m.end() - m.start(), m.start(), m.group(0)) for m in re.finditer(r"[A-Za-z0-9]+", value[lo:hi])]
    runs = [r for r in runs if r[0] >= 3]
    if not runs:
        return _fragments(value)
    run = max(runs)[2]
    n = len(run)
    if n <= 5:
        return [run]
    h = max(5, (n + 1) // 2)
    return [run[:h], run[-h:]]


_HEX_RUN = re.compile(r"[0-9a-f]{6,128}")
_HEX_RUN_B = re.compile(rb"[0-9a-f]{6,128}")


class Scope:
    """한 줄(또는 문서 하나)에서 검사할 알려진 값 번호와 해시 치환 여부. 원문 바이트에서 한 번만 정한다."""
    __slots__ = ("active", "hash_on")

    def __init__(self, active, hash_on):
        self.active = active
        self.hash_on = hash_on


class Redactor:
    def __init__(self, known_values=None, weak_digests=None):
        self.known = []
        self.digest_list = []
        for v in known_values or []:
            ws = windows(v)
            # 값 전체와 긴 조각을 한 정규식으로(전체가 먼저). 이름 붙은 묶음으로 어느 쪽인지 센다.
            full = re.compile("(?P<f>%s)" % variant_pattern(v) + ("|(?P<p>%s)" % "|".join(variant_pattern(w) for w in ws) if ws else ""))
            part = None
            anc = _anchors(v)
            self.known.append((full, part, anc, [x.encode("ascii") for x in anc]))
            self.digest_list += digests(v)
        for ds in weak_digests or []:
            self.digest_list += list(ds)
        self.hashes = hash_prefixes(self.digest_list)
        self.rx = [(kind, re.compile(pat), pre, mode, ci) for kind, pat, pre, mode, ci in PATTERNS]
        self.counts = {}
        self._all = Scope(list(range(len(self.known))), bool(self.hashes))
        # 모든 닻을 한 정규식으로: 문자열마다 한 번 찾아 닻이 하나도 없으면 값 치환을 통째로 건너뛴다.
        anc = sorted({a for k in self.known for a in k[2]}, key=len, reverse=True)
        self._any_anchor = re.compile("|".join(re.escape(a) for a in anc)) if anc else None

    def _bump(self, kind, n):
        if n:
            self.counts[kind] = self.counts.get(kind, 0) + n

    def scope(self, raw):
        """원문(bytes 또는 str)에서 알려진 해시 앞자리가 16진 토큰으로 있는지 본다. 알려진 값은 문자열마다 닻으로 거른다
        (줄 원문에는 정제로 버릴 이미지 base64·중복 사본이 커서 줄 단위로 닻을 찾는 편이 더 느렸다)."""
        isb = isinstance(raw, (bytes, bytearray))
        act = self._all.active
        hon = False
        if self.hashes:
            for m in (_HEX_RUN_B if isb else _HEX_RUN).finditer(raw):
                g = m.group(0)
                if (g.decode("ascii") if isb else g) in self.hashes:
                    hon = True
                    break
        return Scope(act, hon)

    def _known_tok(self, m):
        kind = "known" if m.group("f") is not None else "known_part"
        self._bump(kind, 1)
        return TOKEN % kind

    def _hash_sub(self, s):
        n = [0]

        def f(m):
            if m.group(0) in self.hashes:
                n[0] += 1
                return TOKEN % "known_hash"
            return m.group(0)
        s = _HEX_RX.sub(f, s)
        self._bump("known_hash", n[0])
        return s

    def text(self, s, scope=None):
        """문자열 하나를 가린다. 절단보다 먼저 전체 문자열에 적용해야 한다.
        scope 가 없으면 이 문자열 자체로 정한다(느리지만 같은 결과)."""
        if not s or len(s) < 3:
            return s
        if scope is None:
            scope = self.scope(s) if self.known or self.hashes else self._all
        if self._any_anchor is not None and self._any_anchor.search(s):
            for i in scope.active:
                full, part, anc, _ = self.known[i]
                if not any(x in s for x in anc):
                    continue
                s = full.sub(self._known_tok, s)
        if scope.hash_on and len(s) >= 6:
            s = self._hash_sub(s)
        sl = None
        for kind, rx, pre, mode, ci in self.rx:
            if ci and sl is None:
                sl = ascii_lower(s)
            if not _prefilter_ok(pre, ci, s, sl):
                continue
            s, n = _sub(rx, ci, s, TOKEN % kind, mode)
            if n:
                self._bump(kind, n)
                sl = None
        return s

    def obj(self, o, scope=None):
        """JSON 을 푼 객체 전체를 돌며 문자열 값과 키를 가린다. scope 는 줄 원문에서 한 번 정한 것을 넘긴다.
        값 전체가 uuid·API 식별자(msg_·toolu_·req_) 형태면 비밀이 들어갈 수 없으므로 건너뛴다(식별자 보존)."""
        if scope is None:
            scope = self._all
        if isinstance(o, str):
            if len(o) <= 64 and _ID_RX.match(o):
                return o
            return self.text(o, scope)
        if isinstance(o, list):
            return [self.obj(x, scope) for x in o]
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                kk = self.text(k, scope) if isinstance(k, str) and len(k) >= 6 else k
                out[kk] = self.obj(v, scope)
            return out
        return o

    # -- 수확 ------------------------------------------------------------------
    def harvest_text(self, s, into):
        """가리기 전 문자열에서 비밀번호·토큰 문맥의 값을 모은다(into: 값 → 규칙 종류)."""
        if not s or len(s) < 6:
            return
        if "apikey_" in s:
            for m in _APIKEY_PARTS.finditer(s):
                for part in m.group(1).split("_"):
                    if len(part) >= 12:
                        into.setdefault(part, "apikey_hex")
        sl = ascii_lower(s)
        for kind, rx, pre, mode, ci in self.rx:
            if kind not in HARVEST_KINDS or not _prefilter_ok(pre, ci, s, sl):
                continue
            for m in rx.finditer(sl if ci else s):
                g = _values(m, s)
                if g:
                    into.setdefault(g, kind)

    def harvest_obj(self, o, into):
        if isinstance(o, str):
            self.harvest_text(o, into)
        elif isinstance(o, list):
            for x in o:
                self.harvest_obj(x, into)
        elif isinstance(o, dict):
            for v in o.values():
                self.harvest_obj(v, into)


class Checker:
    """구현 규칙(가림과 같은 PATTERNS)으로 남은 비밀을 센다. 값은 돌려주지 않고 개수만 돌려준다.
    알려진 값 조각은 가림보다 엄격하게(길이 max(8, ceil(0.6×길이)) 이상) 센다.
    바이트 검사도 UTF-8 로 풀어(surrogateescape, 손실 없음) 문자 기준으로 같은 규칙을 적용한다."""

    def __init__(self, known_values=None):
        self.known_s = []
        for v in known_values or []:
            self.known_s.append(("known", re.compile(variant_pattern(v))))
            ws = windows(v, frac=0.6)
            if ws:
                self.known_s.append(("known_part", re.compile("|".join(variant_pattern(w) for w in ws))))
        self.rx_s = [(k, re.compile(p), ci) for k, p, _, _, ci in PATTERNS]

    def count_text(self, s, into, known_only=False):
        if not s:
            return
        for k, rx in self.known_s:
            n = sum(1 for _ in rx.finditer(s))
            if n:
                into[k] = into.get(k, 0) + n
        if known_only:
            return
        sl = ascii_lower(s)
        for k, rx, ci in self.rx_s:
            n = sum(1 for _ in rx.finditer(sl if ci else s))
            if n:
                into[k] = into.get(k, 0) + n

    def count_bytes(self, data, into, known_only=False, chunk=32 << 20, overlap=1 << 16, positions=None, base=0):
        """바이트열을 겹침 창으로 풀어 훑는다. 창의 본 구간에서 시작한 일치만 센다(겹침 중복 없음).
        positions 를 주면 (종류, 파일 바이트 위치) 를 200개까지 담는다(페이지 귀속용)."""
        pos = 0
        n = len(data)
        while pos < n:
            main = data[pos:pos + chunk].decode("utf-8", "surrogateescape")
            ov = data[pos + chunk:pos + chunk + overlap].decode("utf-8", "surrogateescape")
            text = main + ov
            lim = len(main)
            items = [(k, rx, False) for k, rx in self.known_s]
            if not known_only:
                items += self.rx_s
            low = None
            for k, rx, ci in items:
                if ci and low is None:
                    low = ascii_lower(text)
                for m in rx.finditer(low if ci else text):
                    if m.start() >= lim:
                        continue
                    into[k] = into.get(k, 0) + 1
                    if positions is not None and len(positions) < 200:
                        off = len(text[:m.start()].encode("utf-8", "surrogateescape"))
                        positions.append((k, base + pos + off))
            pos += chunk

    def count_bytes_file(self, path, into, known_only=False, positions=None):
        if not os.path.exists(path):
            return 0
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            while True:
                start = fh.tell()
                block = fh.read(64 << 20)
                if not block:
                    break
                tail = fh.read(1 << 16)
                fh.seek(start + len(block))
                self.count_bytes(block + tail, into, known_only=known_only, chunk=len(block), positions=positions,
                                 base=start)
        return size


def freelist_pages(path):
    """SQLite 파일의 빈 페이지(freelist) 번호 목록. 파일 머리 32바이트째에 첫 trunk 페이지가 있다."""
    import struct
    out = []
    if not os.path.exists(path):
        return out, 0
    with open(path, "rb") as fh:
        hdr = fh.read(100)
        if len(hdr) < 100 or not hdr.startswith(b"SQLite format 3\x00"):
            return out, 0
        ps = struct.unpack(">H", hdr[16:18])[0]
        ps = 65536 if ps == 1 else ps
        trunk = struct.unpack(">I", hdr[32:36])[0]
        seen = set()
        while trunk and trunk not in seen:
            seen.add(trunk)
            out.append(trunk)
            fh.seek((trunk - 1) * ps)
            pg = fh.read(ps)
            nxt, cnt = struct.unpack(">II", pg[:8])
            for i in range(min(cnt, (ps - 8) // 4)):
                out.append(struct.unpack(">I", pg[8 + 4 * i:12 + 4 * i])[0])
            trunk = nxt
    return out, ps
