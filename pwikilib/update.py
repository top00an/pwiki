"""pwiki update: bring a git-clone install up to date, then re-run install.sh with the choices recorded at install time.

Steps: fetch the upstream branch → compare versions → (--check stops here) → refuse if tracked files were edited locally →
fast-forward only → re-run install.sh with the recorded collector/hook/skill choices → print the CHANGELOG sections in between.
A feature the user turned off is never turned back on: every choice is passed explicitly (--x or --no-x).
"""
import json
import os
import re
import subprocess

from . import VERSION
from . import paths

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VER_RX = re.compile(r'^VERSION = "([^"]+)"', re.M)
_CL_HEAD_RX = re.compile(r"^## \[?v?(\d+(?:\.\d+)*)\]?.*$", re.M)


def _git(*args, check=True, strip=True):
    r = subprocess.run(["git", "-C", REPO] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if check and r.returncode != 0:
        raise UpdateError("git %s 실패: %s" % (args[0], (r.stderr or r.stdout).strip().splitlines()[-1:] or ["?"]))
    return r.stdout.strip() if strip else r.stdout


class UpdateError(Exception):
    pass


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


def changelog_between(text, old, new):
    """CHANGELOG.md 에서 old 보다 크고 new 이하인 판의 절만(위에서부터 그대로)."""
    out = []
    heads = list(_CL_HEAD_RX.finditer(text or ""))
    for i, m in enumerate(heads):
        v = vtuple(m.group(1))
        if vtuple(old) < v <= vtuple(new):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
            out.append(text[m.start():end].rstrip())
    return "\n\n".join(out)


def install_args(state):
    """설치 기록(install.json) → install.sh 인자. 켜고 끈 선택을 모두 명시해서 끈 기능을 다시 켜지 않는다."""
    args = []
    for k in ("collector", "hook", "skill"):
        args.append("--%s" % k if state.get(k) else "--no-%s" % k)
    if isinstance(state.get("python"), str) and state["python"]:
        args += ["--python", state["python"]]
    # 스킬 자리도 settings 위치에서 정해지므로(install.sh SKILL_DIR) 훅이나 스킬이 켜져 있으면 넘긴다
    if (state.get("hook") or state.get("skill")) and isinstance(state.get("settings"), str) and state["settings"]:
        args += ["--settings", state["settings"]]
    return args


def _upstream():
    up = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    return up if up and "/" in up else "origin/main"


def _local_edits():
    """추적 중인 파일을 고친 것만(새 파일은 fast-forward 를 막지 않는 한 둔다)."""
    # 줄 머리 두 칸이 상태 글자라 앞 공백을 지우면 안 된다
    return [ln[3:] for ln in _git("status", "--porcelain", "--untracked-files=no", strip=False).splitlines()
            if ln.strip()]


def _installed_version():
    """설치 폴더 파일의 판(실행 중인 모듈 값이 아니라 디스크의 HEAD 기준)."""
    try:
        with open(os.path.join(REPO, "pwikilib", "__init__.py"), encoding="utf-8") as fh:
            return _VER_RX.search(fh.read()).group(1)
    except (OSError, AttributeError):
        return VERSION


def run(check_only=False, out=print):
    if not os.path.exists(os.path.join(REPO, ".git")):  # worktree 는 .git 이 파일이다
        raise UpdateError("이 설치는 git clone 이 아니다(%s). 새 판을 받아 다시 풀고 ./install.sh 를 돌린다" % REPO)
    cur = _installed_version()
    up = _upstream()
    remote = up.split("/", 1)[0]
    out("원격 확인: %s" % up)
    _git("fetch", "--quiet", remote)
    head, target = _git("rev-parse", "HEAD"), _git("rev-parse", up)
    try:
        new_ver = _VER_RX.search(_git("show", "%s:pwikilib/__init__.py" % up)).group(1)
    except (UpdateError, AttributeError):
        new_ver = "?"
    # 원격 끝이 이미 지금 HEAD 의 조상이면(같거나 로컬이 앞섬) 받을 것이 없다
    if subprocess.run(["git", "-C", REPO, "merge-base", "--is-ancestor", target, head]).returncode == 0:
        out("최신입니다 (pwiki %s)" % cur)
        return 0
    n = _git("rev-list", "--count", "HEAD..%s" % up)
    out("새 판이 있다: %s → %s (커밋 %s개)" % (cur, new_ver, n))
    if check_only:
        out("--check: 아무것도 바꾸지 않았다. 받으려면: pwiki update")
        return 0
    edits = _local_edits()
    if edits:
        raise UpdateError("설치 폴더에서 고친 파일이 있어 덮어쓰지 않는다: %s\n"
                          "고친 것을 따로 보관하거나(git stash) 되돌린 뒤 다시 돌린다" % ", ".join(edits[:8]))
    r = subprocess.run(["git", "-C", REPO, "merge", "--ff-only", "--quiet", up], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode != 0:
        raise UpdateError("앞으로 감기(fast-forward)가 안 된다: 설치 폴더에 따로 만든 커밋이 있다. "
                          "git -C %s log %s..HEAD 로 확인한다" % (REPO, up))
    out("코드 받음: %s → %s" % (head[:7], target[:7]))
    state_path = os.path.join(paths.pwiki_home(), "install.json")
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = None
    rec = state.get("repo") if isinstance(state, dict) else None
    # 빈 값은 realpath 가 작업 폴더로 바꿔 버리므로 따로 거른다
    if not isinstance(rec, str) or not rec or os.path.realpath(rec) != os.path.realpath(REPO):
        out("설치 기록(%s)이 없거나 다른 폴더의 것이라 설치를 다시 돌리지 않았다. 직접: %s/install.sh"
            % (state_path, REPO))
    else:
        args = install_args(state)
        out("설치 다시 반영: install.sh %s" % " ".join(args))
        env = dict(os.environ)
        if isinstance(state.get("vault"), str) and state["vault"] and "PWIKI_VAULT" not in env:
            env["PWIKI_VAULT"] = state["vault"]  # 설치 때 vault 를 그대로(지금 셸에 값이 없으면 기본값으로 바뀐다)
        rc = subprocess.run(["/bin/bash", os.path.join(REPO, "install.sh")] + args, env=env).returncode
        if rc != 0:
            raise UpdateError("install.sh 가 %d 로 끝났다. 코드는 이미 새 판이다. 위 출력을 보고 install.sh 를 다시 돌린다" % rc)
    try:
        with open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8") as fh:
            notes = changelog_between(fh.read(), cur, new_ver)
    except OSError:
        notes = ""
    out("\n# 바뀐 내용 (%s → %s)\n" % (cur, new_ver))
    out(notes or "(CHANGELOG 에 해당 절이 없다)")
    return 0
