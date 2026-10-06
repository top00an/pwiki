#!/usr/bin/python3
"""Fill the @@…@@ placeholders in the launchd plist template (local.pwiki.collect.plist.in) with the user's paths and write the result.

Usage:
  render_plist.py --python PY --repo DIR --pwiki-home DIR --claude-dir DIR --vault DIR [--out PATH]
  Without --out, writes to stdout. Values are XML-escaped, and the result is read back with plistlib to check it.
Exit codes: 0 pass, 3 check failed (nothing written).
"""
import argparse
import os
import plistlib
import sys
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.realpath(__file__))
TEMPLATE = os.path.join(HERE, "local.pwiki.collect.plist.in")
LABEL = "local.pwiki.collect"
KEYS = ("PYTHON", "REPO", "PWIKI_HOME", "CLAUDE_DIR", "VAULT")


def render(values, template=TEMPLATE):
    with open(template, encoding="utf-8") as fh:
        s = fh.read()
    for k in KEYS:
        v = values[k]
        if not os.path.isabs(v):
            raise ValueError("%s 는 절대 경로여야 한다" % k)
        s = s.replace("@@%s@@" % k, escape(v))
    if "@@" in s:
        raise ValueError("바꾸지 못한 자리가 남았다")
    return s


def check(text, values):
    d = plistlib.loads(text.encode("utf-8"))
    bad = []
    if d.get("Label") != LABEL:
        bad.append("Label")
    if d.get("ProgramArguments") != [values["PYTHON"], os.path.join(values["REPO"], "install", "pwiki_collect.py")]:
        bad.append("ProgramArguments")
    if d.get("WorkingDirectory") != values["REPO"]:
        bad.append("WorkingDirectory")
    env = d.get("EnvironmentVariables") or {}
    for k, name in (("PWIKI_HOME", "PWIKI_HOME"), ("CLAUDE_DIR", "PWIKI_CLAUDE_DIR"), ("VAULT", "PWIKI_VAULT")):
        if env.get(name) != values[k]:
            bad.append("EnvironmentVariables." + name)
    if not isinstance(d.get("StartInterval"), int):
        bad.append("StartInterval")
    return bad


def main(argv=None):
    ap = argparse.ArgumentParser(description="pwiki 수집 plist 를 템플릿에서 만든다")
    ap.add_argument("--python", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--pwiki-home", required=True)
    ap.add_argument("--claude-dir", required=True)
    ap.add_argument("--vault", required=True)
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    values = {"PYTHON": a.python, "REPO": a.repo, "PWIKI_HOME": a.pwiki_home, "CLAUDE_DIR": a.claude_dir,
              "VAULT": a.vault}
    try:
        text = render(values)
        bad = check(text, values)
    except Exception as e:  # 템플릿 오류·XML 오류·경로 오류는 모두 쓰지 않고 멈춘다
        sys.stderr.write("plist 만들기 실패: %s: %s\n" % (type(e).__name__, e))
        return 3
    if bad:
        sys.stderr.write("plist 검사 실패: %s\n" % ", ".join(bad))
        return 3
    if a.out:
        fd = os.open(a.out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
