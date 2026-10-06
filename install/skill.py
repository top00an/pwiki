#!/usr/bin/python3
"""Claude Code 스킬(~/.claude/skills/pwiki/SKILL.md) 설치·제거. 이 저장소가 쓴 스킬만 바꾸고 지운다.

설치: install/skill/SKILL.md.in 의 @CMD@(이 설치의 python 과 pwiki 경로)·@MARK@ 를 채워 <dir>/SKILL.md 로 쓴다(644).
  - 같은 내용이면 그대로 둔다.
  - 이미 있는 SKILL.md(끊긴 링크 포함)가 pwiki 표시 줄이 없는 사람 파일이면 쓰지 않고 멈춘다(rc 3).
  - 다른 자리에 설치한 pwiki 의 스킬이면 쓰지 않고 멈춘다(rc 3). 그 자리에서 제거한 뒤 다시 돌린다.
  - 경로에 스킬 설정 문법을 깨는 글자(줄바꿈 , ( ) " # : -->)가 있으면 설치하지 않는다(rc 3).
  - 쓰기는 같은 폴더에 무작위 이름 임시 파일을 새로 만들어(O_EXCL) 쓴 뒤 바꿔 넣는다. 미리 둔 링크를 따라가지 않는다.
제거: 표시 줄의 repo 가 이 저장소일 때만 SKILL.md 를 지우고, 폴더가 비면 폴더도 지운다.

표시 줄: <!-- pwiki-skill repo="<JSON 문자열>" written by pwiki install.sh; ./uninstall.sh removes it -->
(예전 형식 <!-- pwiki-skill repo=<경로> (written ...) --> 도 읽는다.)

사용: skill.py install|remove --dir DIR --repo REPO [--python PY] [--dry-run]
종료 코드: 0 바꿈 또는 그대로, 3 남의 파일이거나 쓸 수 없는 경로라 건드리지 않음, 2 사용 오류.
"""
import argparse
import json
import os
import re
import shlex
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "skill", "SKILL.md.in")
MARK_RX = re.compile(r'<!-- pwiki-skill repo=("(?:[^"\\]|\\.)*")')
OLD_MARK_RX = re.compile(r"<!-- pwiki-skill repo=(.*) \(written by pwiki install\.sh")
BAD = ("\n", "\r", ",", "(", ")", '"', "#", ":", "-->")


def mark(repo):
    return "<!-- pwiki-skill repo=%s written by pwiki install.sh; ./uninstall.sh removes it -->" % json.dumps(repo)


def render(py, repo):
    with open(TEMPLATE, encoding="utf-8") as fh:
        t = fh.read()
    cmd = "%s %s" % (shlex.quote(py), shlex.quote(os.path.join(repo, "pwiki")))
    return t.replace("@CMD@", cmd).replace("@MARK@", mark(repo))


def owner(path):
    """파일의 pwiki 표시 줄이 가리키는 저장소. 표시 줄이 없거나 읽을 수 없으면 None."""
    try:
        with open(path, encoding="utf-8") as fh:
            head = fh.read(4096)
    except (OSError, ValueError):
        return None
    m = MARK_RX.search(head)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            return None
    m = OLD_MARK_RX.search(head)
    return m.group(1) if m else None


def install(d, repo, py, dry):
    bad = [c for c in BAD if c in repo or c in py]
    if bad:
        print("멈춤: 경로에 스킬 설정에 쓸 수 없는 글자(%s)가 있다. 스킬은 건너뛴다" % " ".join(repr(c) for c in bad))
        return 3
    path = os.path.join(d, "SKILL.md")
    body = render(py, repo)
    if os.path.lexists(path):
        if os.path.islink(path) and not os.path.exists(path):
            print("멈춤: %s 는 끊긴 링크다(pwiki 가 만든 것이 아니라 건드리지 않음)" % path)
            return 3
        who = owner(path)
        if who is None:
            print("멈춤: %s 는 pwiki 가 만든 파일이 아니다(건드리지 않음)" % path)
            return 3
        if who != repo:
            print("멈춤: %s 는 다른 자리(%s)에 설치한 pwiki 의 스킬이다. 그 자리에서 제거한 뒤 다시 돌린다" % (path, who))
            return 3
        with open(path, encoding="utf-8") as fh:
            if fh.read() == body:
                print("스킬 같음: %s" % path)
                return 0
    print("+ (쓰기) %s" % path)
    if dry:
        return 0
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".SKILL.md.", suffix=".pwiki-tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            os.fchmod(fh.fileno(), 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return 0


def remove(d, repo, dry):
    path = os.path.join(d, "SKILL.md")
    if not os.path.lexists(path):
        print("스킬 없음: %s (건너뜀)" % path)
        return 0
    who = None if os.path.islink(path) else owner(path)
    if who != repo:
        print("스킬 %s 는 이 저장소(%s)가 만든 것이 아니다. 건드리지 않는다" % (path, repo))
        return 3
    print("+ rm %s" % path)
    if dry:
        return 0
    os.remove(path)
    try:
        os.rmdir(d)
    except OSError:
        pass
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("install", "remove"))
    ap.add_argument("--dir", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if a.action == "install":
        return install(a.dir, a.repo, a.python, a.dry_run)
    return remove(a.dir, a.repo, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
