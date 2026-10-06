"""Path settings. Tests swap every one of them through environment variables."""
import os
import re
import datetime

HOME_USER = os.path.expanduser("~")


def claude_dir():
    return os.environ.get("PWIKI_CLAUDE_DIR") or os.path.join(HOME_USER, ".claude")


def codex_dir():
    """OpenAI Codex CLI 기록 폴더. 여기서는 sessions/**/rollout-*.jsonl 과 history.jsonl 만 연다(ingest.discover_codex)."""
    return os.environ.get("PWIKI_CODEX_DIR") or os.path.join(HOME_USER, ".codex")


# 원천 상대경로(files.path·docs.path). Claude Code 원천은 ~/.claude 기준 그대로, Codex 원천은 앞에 "codex/" 를 붙인다.
CODEX_PREFIX = "codex/"


def source_of(rel):
    """상대경로의 원천: 'codex' 또는 'claude'."""
    return "codex" if (rel or "").startswith(CODEX_PREFIX) else "claude"


def source_path(rel, claude=None):
    """상대경로 → 절대경로. claude 를 주면 Claude Code 원천의 기준 폴더로 쓴다."""
    if source_of(rel) == "codex":
        return os.path.join(codex_dir(), rel[len(CODEX_PREFIX):])
    return os.path.join(claude or claude_dir(), rel)


def pwiki_home():
    return os.environ.get("PWIKI_HOME") or os.path.join(HOME_USER, ".pwiki")


def vault_dir():
    return os.environ.get("PWIKI_VAULT") or os.path.join(HOME_USER, "pwiki")


def db_path():
    return os.path.join(pwiki_home(), "pwiki.db")


def secrets_path():
    return os.path.join(pwiki_home(), "secrets.local")


def harvest_path():
    return os.path.join(pwiki_home(), "secrets.harvested")


def lock_path():
    return os.path.join(pwiki_home(), "ingest.lock")


def logs_dir():
    return os.path.join(pwiki_home(), "logs")


def runs_dir():
    return os.path.join(pwiki_home(), "runs")


def ensure_home():
    h = pwiki_home()
    os.makedirs(h, mode=0o700, exist_ok=True)
    try:
        os.chmod(h, 0o700)
    except OSError:
        pass
    for d in (logs_dir(), runs_dir()):
        os.makedirs(d, mode=0o700, exist_ok=True)
    return h


KST = datetime.timezone(datetime.timedelta(hours=9))

# 표시 시간대. 저장은 UTC 이고 날짜 경계·표시만 이 시간대를 따른다(DB 를 다시 만들 필요 없음).
# PWIKI_TZ: 비우면 KST(기존 동작 그대로) · local(시스템 시간대) · UTC · +05:30 같은 고정 오프셋 · Asia/Tokyo 같은 IANA 이름.
# 알아보지 못하는 값이면 KST 로 둔다(훅이 실패하면 안 된다).
_OFF_RX = re.compile(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", re.I)
_tz_cache = {}


def _resolve_tz(v):
    v = (v or "").strip()
    if not v or v.upper() == "KST":
        return KST, "KST"
    if v.lower() == "local":
        return None, datetime.datetime.now().astimezone().tzname() or "local"
    if v.upper() in ("UTC", "GMT", "Z"):
        return datetime.timezone.utc, "UTC"
    m = _OFF_RX.match(v)
    if m:
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if h <= 14 and mi < 60:
            sign = -1 if m.group(1) == "-" else 1
            return datetime.timezone(sign * datetime.timedelta(hours=h, minutes=mi)), "UTC%s%02d:%02d" % (m.group(1), h, mi)
    try:
        import zoneinfo
        z = zoneinfo.ZoneInfo(v)
        return z, datetime.datetime.now(tz=z).tzname() or v
    except Exception:
        return KST, "KST"


def display_tz():
    """(tzinfo 또는 None=시스템 시간대, 표시 이름)."""
    v = os.environ.get("PWIKI_TZ", "")
    if v not in _tz_cache:
        _tz_cache[v] = _resolve_tz(v)
    return _tz_cache[v]


def tz_label(at=None):
    """표시 시간대 이름. 서머타임이 있는 시간대(IANA 이름·local)는 at(datetime, 없으면 지금) 시각의 이름을 쓴다."""
    z, label = display_tz()
    if at is None or not (z is None or type(z).__name__ == "ZoneInfo"):
        return label
    return _to_display(at).tzname() or label


def tz_label_for_date(date_str):
    """그날 정오 기준 이름(하루 머리말용)."""
    try:
        n = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(hour=12)
    except (TypeError, ValueError):
        return tz_label()
    z = display_tz()[0]
    return tz_label(n.astimezone() if z is None else n.replace(tzinfo=z))


def _to_display(d):
    z = display_tz()[0]
    return d.astimezone(z) if z is not None else d.astimezone()


def parse_ts(ts):
    """기록의 UTC ISO 문자열(또는 epoch ms)을 aware datetime 으로. 실패하면 None."""
    if ts is None or ts == "":
        return None
    try:
        if isinstance(ts, (int, float)) or (isinstance(ts, str) and ts.isdigit()):
            v = float(ts)
            if v > 1e11:
                v = v / 1000.0
            return datetime.datetime.fromtimestamp(v, tz=datetime.timezone.utc)
        s = str(ts)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = datetime.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d
    except (ValueError, OverflowError, OSError):
        return None


def to_utc_iso(ts):
    d = parse_ts(ts)
    if d is None:
        return None
    return d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def kst(ts):
    """표시 시간대(PWIKI_TZ, 기본 KST)로 바꾼 datetime. 이름은 예전 그대로 둔다."""
    d = parse_ts(ts)
    return _to_display(d) if d else None


def kst_str(ts, fmt="%Y-%m-%d %H:%M"):
    d = kst(ts)
    return d.strftime(fmt) if d else "-"


def kst_str_z(ts, fmt="%Y-%m-%d %H:%M"):
    """kst_str 뒤에 표시 시간대 이름을 붙인다. 기본 'YYYY-MM-DD HH:MM KST'."""
    d = parse_ts(ts)
    return "%s %s" % (kst_str(ts, fmt), tz_label(d) if d else tz_label())


def kst_date(ts):
    d = kst(ts)
    return d.strftime("%Y-%m-%d") if d else None


def kst_day_bounds_utc(date_str):
    """표시 시간대 날짜 하루의 [시작, 끝) 을 UTC ISO 문자열로. 서머타임이 있는 시간대는 그날의 실제 길이를 쓴다."""
    z = display_tz()[0]
    n = datetime.datetime.strptime(date_str, "%Y-%m-%d")
    n2 = n + datetime.timedelta(days=1)
    if z is None:
        d, e = n.astimezone(), n2.astimezone()
    else:
        d, e = n.replace(tzinfo=z), n2.replace(tzinfo=z)
    f = lambda x: x.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return f(d), f(e)


def today_kst():
    return _to_display(datetime.datetime.now(tz=datetime.timezone.utc)).strftime("%Y-%m-%d")


def now_utc_iso():
    return datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
