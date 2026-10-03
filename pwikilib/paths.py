"""경로 설정. 시험은 환경변수로 전부 바꿔 끼운다."""
import os
import datetime

HOME_USER = os.path.expanduser("~")


def claude_dir():
    return os.environ.get("PWIKI_CLAUDE_DIR") or os.path.join(HOME_USER, ".claude")


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
    d = parse_ts(ts)
    return d.astimezone(KST) if d else None


def kst_str(ts, fmt="%Y-%m-%d %H:%M"):
    d = kst(ts)
    return d.strftime(fmt) if d else "-"


def kst_date(ts):
    d = kst(ts)
    return d.strftime("%Y-%m-%d") if d else None


def kst_day_bounds_utc(date_str):
    """KST 날짜 하루의 [시작, 끝) 을 UTC ISO 문자열로."""
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=KST)
    e = d + datetime.timedelta(days=1)
    f = lambda x: x.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return f(d), f(e)


def today_kst():
    return datetime.datetime.now(tz=KST).strftime("%Y-%m-%d")


def now_utc_iso():
    return datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
