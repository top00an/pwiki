#!/usr/bin/python3
"""Collection wrapper that launchd calls periodically (local.pwiki.collect).

What it does: runs pwiki ingest --all once and records the result.
- Locking: uses pwiki ingest's own lock (~/.pwiki/ingest.lock, flock). If another collection is running, ingest exits with 75;
  this wrapper logs 'busy' and exits 0 (not a failure: the other run does the work). launchd also never overlaps the same job.
- Retry: a failure is not retried here. launchd StartInterval calls again on the next round (rounds missed during sleep run once
  after waking).
- Time limit: after PWIKI_COLLECT_TIMEOUT seconds (default 3300), SIGTERM to the process group, SIGKILL 30 seconds later. Logged as 'timeout'.
  If ingest stops midway, the next run resumes reading and redacts again (per transaction).
- Records (all in ~/.pwiki/logs, mode 600):
  collect.log          one JSON line per round (at, outcome, rc, elapsed_s, fails_in_row). Rotated to collect.log.1 past 2MB.
  collect.last.txt     full ingest output of the last round (only post-redaction numbers and paths appear).
  collect.status.json  summary of the last round (for the menu bar and checks).
  collect.launchd.log  launchd stdout/stderr (only failures before this wrapper starts end up here).
- Exit codes: ok and busy are 0, mismatch (line-count reconciliation mismatch) is 2, timeout is 124, anything else is ingest's code (1 if none).

Environment: PWIKI_HOME, PWIKI_CLAUDE_DIR (read by pwiki), PWIKI_BIN (pwiki executable, default this repository's pwiki),
PWIKI_COLLECT_TIMEOUT (seconds).
"""
import json
import os
import signal
import subprocess
import sys
import time

LOG_MAX_BYTES = 2000000
BUSY_RC = 75


def repo():
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def pwiki_home():
    return os.environ.get("PWIKI_HOME") or os.path.join(os.path.expanduser("~"), ".pwiki")


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def write_private(path, data):
    tmp = path + ".tmp%d" % os.getpid()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def append_private(path, line):
    try:
        if os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)


def read_status(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def run_ingest(timeout):
    pw = os.environ.get("PWIKI_BIN")
    cmd = [pw, "ingest", "--all"] if pw else [sys.executable, os.path.join(repo(), "pwiki"), "ingest", "--all"]
    env = dict(os.environ)
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("LANG", "en_US.UTF-8")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                         env=env, cwd=repo(), start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out, False
    except subprocess.TimeoutExpired:
        for sig, wait in ((signal.SIGTERM, 30), (signal.SIGKILL, 10)):
            try:
                os.killpg(p.pid, sig)
            except OSError:
                pass
            try:
                out, _ = p.communicate(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                out = b""
        return p.returncode, out or b"", True


def main():
    os.umask(0o077)
    home = pwiki_home()
    logs = os.path.join(home, "logs")
    os.makedirs(logs, mode=0o700, exist_ok=True)
    timeout = float(os.environ.get("PWIKI_COLLECT_TIMEOUT") or "3300")
    t0 = time.time()
    started = now_iso()
    try:
        rc, out, timed_out = run_ingest(timeout)
    except Exception as e:  # 실행 파일이 없거나 띄우지 못함
        rc, out, timed_out = 1, ("감싸개 오류: %s" % type(e).__name__).encode("utf-8"), False
    elapsed = round(time.time() - t0, 1)
    if timed_out:
        outcome, code = "timeout", 124
    elif rc == 0:
        outcome, code = "ok", 0
    elif rc == BUSY_RC:
        outcome, code = "busy", 0
    elif rc == 2:
        outcome, code = "mismatch", 2
    else:
        outcome, code = "fail", rc if isinstance(rc, int) and rc > 0 else 1
    status_path = os.path.join(logs, "collect.status.json")
    prev = read_status(status_path)
    fails = 0 if outcome in ("ok", "busy") else int(prev.get("fails_in_row") or 0) + 1
    rec = {"at": started, "outcome": outcome, "rc": rc, "elapsed_s": elapsed, "fails_in_row": fails}
    try:
        append_private(os.path.join(logs, "collect.log"), json.dumps(rec, ensure_ascii=False))
        if outcome != "busy":
            head = "# pwiki collect %s · outcome %s · rc %s · %.1f초\n" % (started, outcome, rc, elapsed)
            write_private(os.path.join(logs, "collect.last.txt"), head.encode("utf-8") + (out or b""))
        st = dict(prev)
        st.update({"last_at": started, "last_outcome": outcome, "last_rc": rc, "last_elapsed_s": elapsed,
                   "fails_in_row": fails})
        if outcome == "ok":
            st["last_ok_at"] = started
        write_private(status_path, json.dumps(st, ensure_ascii=False, indent=1).encode("utf-8"))
    except OSError:
        pass
    return code


if __name__ == "__main__":
    sys.exit(main())
