#!/usr/bin/python3
"""Install or remove the Claude Code skill (~/.claude/skills/pwiki/SKILL.md). Only changes or deletes a skill this repository wrote.

Install: fills @CMD@ (this install's python and pwiki path) and @MARK@ in install/skill/SKILL.md.in and writes <dir>/SKILL.md (644).
  - If the content is identical, leaves it alone.
  - If an existing SKILL.md (broken link included) is a person's file without the pwiki marker line, does not write and stops (rc 3).
  - If it is the skill of a pwiki installed elsewhere, does not write and stops (rc 3). Remove it from there, then run again.
  - If a path contains characters that break skill config syntax (newline , ( ) " # : -->), does not install (rc 3).
  - Writes a new randomly named temp file in the same folder (O_EXCL), then replaces. Pre-placed links are not followed.
Remove: deletes SKILL.md only when the marker line's repo is this repository, and deletes the folder too if it becomes empty.

Marker line: <!-- pwiki-skill repo="<JSON string>" written by pwiki install.sh; ./uninstall.sh removes it -->
(The old form <!-- pwiki-skill repo=<path> (written ...) --> is also read.)

Usage: skill.py install|remove --dir DIR --repo REPO [--python PY] [--dry-run]
Exit codes: 0 changed or unchanged, 3 left untouched because the file belongs to someone else or the path cannot be written, 2 usage error.
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
