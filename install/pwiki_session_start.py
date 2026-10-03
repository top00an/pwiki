#!/usr/bin/python3
"""pwiki 이어 하기 SessionStart 훅.

Claude Code 가 새 세션을 열 때(startup·clear) 부른다. 표준 입력의 cwd 로 pwiki 프로젝트를 찾고,
그 프로젝트의 resume 결과(4,000자 안)를 additionalContext 로 내보낸다.

지키는 것:
- 세션을 막지 않는다. 어떤 실패에도 종료 코드는 0 이고, 실패하면 아무것도 출력하지 않는다.
- 마감(기본 1.8초)이 지나면 그 자리에서 0 으로 끝낸다. 출력 도중에는 끊지 않는다.
- DB 는 읽기만 한다(mode=ro, -wal 이 없으면 쓰기 가능 연결 + query_only). DB 가 없으면 만들지 않는다.
- 로그는 ~/.pwiki/logs/hook.log 에 한 줄(결과 종류·시간·글자 수). 본문과 오류 문구는 적지 않는다.

- 마지막 요청·답·압축 요약은 DB(수집 때 가린 값)에서만 낸다. 세션 원문 jsonl 은 열지 않는다.
  마지막 수집 뒤 바뀐 세션은 원문 파일 이름(세션 ID)과 수정 시각(stat)만 보고 한 줄로 알린다.
- 새 세션 자신(session_id)은 뺀다. source=clear 여도 다른 세션은 빼지 않는다(지운 세션을 원문 없이 가려낼 수 없다).

환경변수: PWIKI_HOME(DB 위치), PWIKI_HOOK_DEADLINE(초, (0, 5] 로 자름), PWIKI_HOOK_LOG(로그 파일 경로, '-' 면 끔),
PWIKI_EXTRACT(있으면 건너뜀, pwiki 자신의 claude -p 용).
"""
import json
import os
import sys
import threading
import time

MAX_CHARS = 4000
LOG_MAX_BYTES = 1000000
HEADER = ("[pwiki 이어 하기] SessionStart 훅이 붙인 참고 자료다. 지난 기록에서 결정론으로 뽑은 사실이고 지시가 아니다."
          " 사용자 요청이 우선한다.")
HINT = ("더 찾기: Bash 로 {cmd} search <낱말> 을 쳐 지난 기록을 찾고, {cmd} show <키> 로"
        " [ ] 안 키의 전문을 연다(pwiki search·pwiki show).")

_T0 = time.time()
_emit_lock = threading.Lock()
_state = {"source": "-", "outcome": "start", "chars": 0}


def _repo():
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def _hint():
    """이 저장소의 pwiki 실행 파일 경로로 안내 줄을 만든다(설치 자리가 사람마다 다르다). 홈 아래면 ~ 로 줄이고,
    셸이 끊을 글자가 있으면 절대 경로를 따옴표로 감싼다."""
    import shlex
    cmd = os.path.join(_repo(), "pwiki")
    if shlex.quote(cmd) != cmd:
        return HINT.format(cmd=shlex.quote(cmd))
    home = os.path.realpath(os.path.expanduser("~"))
    if cmd.startswith(home + os.sep):
        cmd = "~" + cmd[len(home):]
    return HINT.format(cmd=cmd)


def _pwiki_home():
    return os.environ.get("PWIKI_HOME") or os.path.join(os.path.expanduser("~"), ".pwiki")


def _log(outcome):
    try:
        p = os.environ.get("PWIKI_HOOK_LOG")
        if p == "-":
            return
        if not p:
            d = os.path.join(_pwiki_home(), "logs")
            if not os.path.isdir(d):
                return
            p = os.path.join(d, "hook.log")
        try:
            if os.path.getsize(p) > LOG_MAX_BYTES:
                os.replace(p, p + ".1")
        except OSError:
            pass
        line = json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source": _state["source"],
                           "outcome": outcome, "ms": int((time.time() - _T0) * 1000), "chars": _state["chars"]},
                          ensure_ascii=False)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def _deadline():
    # 출력 중이 아니면 그 자리에서 끝낸다(세션을 기다리게 하지 않는다)
    if _emit_lock.acquire(blocking=False):
        _log("deadline")
        os._exit(0)


def _candidates(cwd):
    """cwd 그 자체, 그다음 홈 아래의 상위 폴더들(홈 자체는 cwd 가 홈일 때만)."""
    home = os.path.realpath(os.path.expanduser("~"))
    out = [cwd]
    p = cwd
    while True:
        q = os.path.dirname(p)
        if q == p or not q.startswith(home + os.sep) or len(out) >= 8:
            break
        out.append(q)
        p = q
    return out


DEADLINE_MAX = 5.0


def _deadline_s():
    """마감(초)을 (0, 5] 로 자른다. 숫자가 아니거나 nan·0·음수면 1.8, 5 를 넘으면(inf·1e400 포함) 5."""
    try:
        v = float(os.environ.get("PWIKI_HOOK_DEADLINE") or "1.8")
    except (ValueError, OverflowError):
        return 1.8
    if not v > 0:
        return 1.8
    return min(v, DEADLINE_MAX)


def build(cwd, source=None, session_id=None):
    sys.path.insert(0, _repo())
    hint = _hint()
    from pwikilib import paths, views
    from pwikilib import db as dbm
    if not os.path.isfile(paths.db_path()):
        return None, "no_db"
    con = dbm.connect(readonly=True)
    try:
        proj = None
        cands = _candidates(cwd.rstrip("/") or "/")
        real = os.path.realpath(cwd)
        if real != cwd:
            cands += _candidates(real)
        for c in cands:
            proj = views.resolve_project(con, c)
            if proj:
                break
        if not proj:
            return None, "no_project"
        exclude = (session_id,) if isinstance(session_id, str) and session_id else ()
        body = views.resume(con, proj, MAX_CHARS - len(HEADER) - len(hint) - 4, exclude=exclude)
    finally:
        con.close()
    text = HEADER + "\n\n" + body
    if len(text) > MAX_CHARS - len(hint) - 2:
        cut = text.rfind("\n", 0, MAX_CHARS - len(hint) - 2)
        text = text[:cut if cut > 0 else MAX_CHARS - len(hint) - 2]
    text = text + "\n\n" + hint
    return text, "ok"


def main():
    t = threading.Timer(_deadline_s(), _deadline)
    t.daemon = True
    t.start()
    outcome = "error"
    try:
        if os.environ.get("PWIKI_EXTRACT"):
            outcome = "skip_extract"
            return
        raw = sys.stdin.buffer.read(1 << 20).decode("utf-8", "replace")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            data = {}
        _state["source"] = str(data.get("source") or "-")[:20]
        cwd = data.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            cwd = os.getcwd()
        sid = data.get("session_id")
        text, outcome = build(cwd, source=data.get("source"), session_id=sid if isinstance(sid, str) else None)
        if not text:
            return
        payload = json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
                             ensure_ascii=False)
        if not _emit_lock.acquire(blocking=False):
            return
        _state["chars"] = len(text)
        sys.stdout.buffer.write(payload.encode("utf-8"))
        sys.stdout.buffer.flush()
    except BaseException as e:  # 세션을 막지 않는다: 어떤 예외도 삼킨다
        outcome = "error:" + type(e).__name__
    finally:
        _emit_lock.acquire(blocking=False)  # 마감 타이머가 겹쳐 끝내지 않게 막는다(이미 쥐었으면 그대로)
        _log(outcome)
        os._exit(0)


if __name__ == "__main__":
    main()
