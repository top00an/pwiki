#!/usr/bin/python3
"""launchd 가 주기마다 부르는 수집 감싸개(local.pwiki.collect).

하는 일: pwiki ingest --all 을 한 번 돌리고 결과를 기록한다.
- 잠금: pwiki ingest 자신의 잠금(~/.pwiki/ingest.lock, flock)을 쓴다. 다른 수집이 돌고 있으면 ingest 가 75 로 끝나고,
  여기서는 'busy' 로 적고 0 으로 끝낸다(실패가 아니다. 그 수집이 일을 한다). launchd 도 같은 잡을 겹쳐 띄우지 않는다.
- 재시도: 실패해도 따로 다시 돌리지 않는다. launchd StartInterval 이 다음 회차에 다시 부른다(잠자는 동안 놓친 회차는 깨어난 뒤 한 번).
- 시간 상한: PWIKI_COLLECT_TIMEOUT 초(기본 3300)가 지나면 프로세스 묶음에 SIGTERM, 30초 뒤 SIGKILL. 'timeout' 으로 적는다.
  ingest 는 중간에 멈춰도 다음 실행이 이어 읽고 다시 가린다(트랜잭션 단위).
- 기록(모두 ~/.pwiki/logs, 600):
  collect.log          회차마다 JSON 한 줄(at·outcome·rc·elapsed_s·fails_in_row). 2MB 넘으면 collect.log.1 로 돌린다.
  collect.last.txt     마지막 회차의 ingest 출력 전체(값은 가린 뒤의 숫자·경로만 나온다).
  collect.status.json  마지막 회차 요약(메뉴바·점검용).
  collect.launchd.log  launchd 의 표준 출력·오류(이 감싸개가 뜨기 전 실패만 남는다).
- 종료 코드: ok·busy 는 0, mismatch(줄 수 대조 불일치)는 2, timeout 은 124, 그 밖은 ingest 의 코드(없으면 1).

환경변수: PWIKI_HOME, PWIKI_CLAUDE_DIR(pwiki 가 읽음), PWIKI_BIN(pwiki 실행 파일, 기본 이 저장소의 pwiki),
PWIKI_COLLECT_TIMEOUT(초).
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
