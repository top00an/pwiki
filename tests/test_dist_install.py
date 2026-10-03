"""배포 설치기·제거기·누출 검사·배포본 생성기 시험.

임시 HOME 에 합성 기록(tools/make_fixture.py)을 만들고 install.sh·uninstall.sh 를 돌린다.
실제 ~/.claude·~/.pwiki·settings.json·launchd 는 건드리지 않는다. 자동 수집은 --dry-run 으로만 보고,
PWIKI_LAUNCHCTL 에는 불리면 표시 파일을 남기고 실패하는 가짜를 준다(불리지 않았는지 끝에 확인한다).
임시 폴더 위치는 PWIKI_TEST_TMP 로 바꿀 수 있다."""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT

INSTALL_SH = os.path.join(ROOT, "install.sh")
UNINSTALL_SH = os.path.join(ROOT, "uninstall.sh")
FIXTURE = os.path.join(ROOT, "tools", "make_fixture.py")
LEAK = os.path.join(ROOT, "tools", "leak_check_dist.py")
MAKE_DIST = os.path.join(ROOT, "tools", "make_dist.sh")
HOOK = os.path.join(ROOT, "install", "pwiki_session_start.py")
RENDER = os.path.join(ROOT, "install", "render_plist.py")
PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
DARWIN = sys.platform == "darwin"

OTHER_SETTINGS = {
    "env": {"OTHER_TOOL_MODE": "quiet"},
    "permissions": {"allow": ["Bash(ls:*)"], "deny": []},
    "hooks": {
        "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "/opt/other-app/start.sh"}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/guard/check.py", "timeout": 10}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "node /opt/app/hook.js stop", "async": True}]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": "/opt/review/한글 경로.sh"}]}],
    },
    "statusLine": {"type": "command", "command": "/opt/status.sh"},
}


def rd(p, binary=False):
    with open(p, "rb" if binary else "r", **({} if binary else {"encoding": "utf-8"})) as fh:
        return fh.read()


def sha(p):
    return hashlib.sha1(rd(p, True)).hexdigest()


class DistBase(unittest.TestCase):
    def setUp(self):
        base = os.environ.get("PWIKI_TEST_TMP") or None
        self.tmp = tempfile.mkdtemp(prefix="home_", dir=base)
        self.home = os.path.join(self.tmp, "h")
        os.makedirs(self.home, mode=0o700)
        self.marker = os.path.join(self.tmp, "launchctl-called")
        self.fake_launchctl = os.path.join(self.tmp, "fake-launchctl")
        with open(self.fake_launchctl, "w") as fh:
            fh.write("#!/bin/sh\necho \"$@\" >> '%s'\nexit 97\n" % self.marker)
        os.chmod(self.fake_launchctl, 0o700)
        self.outputs = []

    def tearDown(self):
        self.assertFalse(os.path.exists(self.marker), "launchctl 이 불렸다")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def env(self, **kw):
        e = {"HOME": self.home, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
             "PWIKI_LAUNCHCTL": self.fake_launchctl, "TMPDIR": self.tmp}
        e.update(kw)
        return e

    def sh(self, args, stdin=None, _cwd=None, **kw):
        p = subprocess.run(args, env=self.env(**kw), capture_output=True, timeout=180, cwd=_cwd,
                           input=stdin.encode("utf-8") if isinstance(stdin, str) else (stdin or b""))
        out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
        self.outputs.append(out)
        return p.returncode, out

    def fixture(self):
        self.info_path = os.path.join(self.tmp, "fixture.json")
        rc, out = self.sh([PY, FIXTURE, "--claude-dir", os.path.join(self.home, ".claude"),
                           "--secrets-out", self.info_path, "--cwd-root", "/home/tester"])
        self.assertEqual(rc, 0, out)
        self.info = json.loads(rd(self.info_path))
        return self.info

    def install(self, *args, **kw):
        return self.sh(["/bin/bash", INSTALL_SH] + list(args), **kw)

    def uninstall(self, *args, **kw):
        return self.sh(["/bin/bash", UNINSTALL_SH] + list(args), **kw)

    def pwiki(self, *args):
        return self.sh([PY, os.path.join(ROOT, "pwiki")] + list(args))

    @property
    def pwiki_home(self):
        return os.path.join(self.home, ".pwiki")

    @property
    def vault(self):
        return os.path.join(self.home, "pwiki")

    def snapshot(self):
        con = sqlite3.connect(os.path.join(self.pwiki_home, "pwiki.db"))
        try:
            counts = tuple(con.execute("SELECT count(*) FROM %s" % t).fetchone()[0] for t in ("events", "cards", "docs"))
        finally:
            con.close()
        files = {}
        for dp, dn, fn in os.walk(self.vault):
            for f in fn:
                ap = os.path.join(dp, f)
                files[os.path.relpath(ap, self.vault)] = sha(ap)
        return counts, files


class InstallUseUninstallTest(DistBase):
    def test_install_use_reinstall_uninstall_keeps_data(self):
        info = self.fixture()
        sec = info["secrets"]
        # 이미 있던 secrets.local 은 그대로 둔다(아는 비밀값 하나를 적어 둔다)
        os.makedirs(self.pwiki_home, mode=0o700)
        sl = os.path.join(self.pwiki_home, "secrets.local")
        fd = os.open(sl, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, ("# 시험\n%s\n" % sec["known"]).encode("utf-8"))
        os.close(fd)
        sl_sha = sha(sl)

        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 0, out)
        for s in ("## 1. 요건", "FTS5 trigram 있음", "## 3. 수집", "# ingest", "## 4. vault", "자동 수집: 건너뜀",
                  "이어 하기 훅: 건너뜀", "secrets.local 있음"):
            self.assertIn(s, out)
        self.assertEqual(os.stat(self.pwiki_home).st_mode & 0o777, 0o700)
        self.assertEqual(sha(sl), sl_sha)
        self.assertEqual(os.stat(sl).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "settings.json")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "Library", "LaunchAgents")))
        self.assertTrue(os.path.isfile(os.path.join(self.vault, "_notes", "README.md")))

        # 명령들
        rc, out = self.pwiki("today")
        self.assertEqual(rc, 0, out)
        for q in info["prompts"]:
            self.assertIn(q[:12], out)
        rc, out = self.pwiki("search", "재설정")
        self.assertEqual(rc, 0, out)
        self.assertIn("결과", out)
        alpha = info["projects"]["alpha"]["cwd"]
        rc, out = self.pwiki("resume", "--project", alpha)
        self.assertEqual(rc, 0, out)
        self.assertIn("# 이어 하기", out)
        self.assertIn(info["prompts"][3][:12], out)
        rc, out = self.pwiki("verify")
        self.assertEqual(rc, 0, out)
        rc, out = self.pwiki("redact-check")
        self.assertEqual(rc, 0, out)
        for v in sec.values():
            rc, out = self.pwiki("search", v)
            self.outputs.pop()  # 검색 출력 머리에 검색어가 다시 찍힌다(값 노출 판정에서 뺀다)
            self.assertNotEqual(rc, 0, "가린 값이 검색된다")

        # 훅을 직접 부른다(설치하지 않고)
        rc, out = self.sh([PY, HOOK], stdin=json.dumps({"cwd": alpha, "source": "startup"}))
        self.assertEqual(rc, 0, out)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith("[pwiki 이어 하기]"))
        self.assertLessEqual(len(ctx), 4000)
        rc, out = self.sh([PY, HOOK], stdin=json.dumps({"cwd": "/nowhere", "source": "startup"}))
        self.assertEqual((rc, out), (0, ""))

        # 가짜 비밀값: 출력·DB·vault·로그 어디에도 없다(수확값 저장소·secrets.local 은 값을 담는 곳이라 뺀다)
        self.assert_no_secrets(sec)

        # 다시 설치: 같은 결과
        snap = self.snapshot()
        state = rd(os.path.join(self.pwiki_home, "install.json"))
        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("새 행 0", out)
        self.assertNotIn("+ mkdir", out)
        self.assertEqual(self.snapshot(), snap)
        self.assertEqual(rd(os.path.join(self.pwiki_home, "install.json")), state)
        self.assertEqual(sha(sl), sl_sha)

        # 제거: 데이터는 남긴다
        rc, out = self.uninstall("--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("남김", out)
        self.assertTrue(os.path.isfile(os.path.join(self.pwiki_home, "pwiki.db")))
        self.assertEqual(sha(sl), sl_sha)
        self.assertEqual(self.snapshot()[1], snap[1])
        self.assertFalse(os.path.exists(os.path.join(self.pwiki_home, "install.json")))
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "settings.json")))
        self.assert_no_secrets(sec)

    def assert_no_secrets(self, sec):
        db = os.path.join(self.pwiki_home, "pwiki.db")
        con = sqlite3.connect(db)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        keep = {os.path.join(self.pwiki_home, "secrets.local"), os.path.join(self.pwiki_home, "secrets.harvested")}
        files = []
        for top in (self.pwiki_home, self.vault):
            for dp, dn, fn in os.walk(top):
                files += [os.path.join(dp, f) for f in fn if os.path.join(dp, f) not in keep]
        self.assertTrue(any(f.endswith("pwiki.db") for f in files))
        for name, v in sec.items():
            for f in files:
                self.assertNotIn(v.encode("utf-8"), rd(f, True), "%s 가 %s 에 남았다" % (name, os.path.basename(f)))
            for o in self.outputs:
                self.assertNotIn(v, o, "%s 가 출력에 나왔다" % name)

    def test_purge_data_needs_confirmation(self):
        self.fixture()
        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 0, out)
        rc, out = self.uninstall("--purge-data")
        self.assertEqual(rc, 2, out)
        self.assertTrue(os.path.isdir(self.pwiki_home))
        rc, out = self.uninstall("--purge-data", "--yes", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("+ rm -f", out)
        self.assertNotIn("rm -rf", out)
        self.assertTrue(os.path.isdir(self.pwiki_home) and os.path.isdir(self.vault))
        rc, out = self.uninstall("--purge-data", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.pwiki_home))
        self.assertFalse(os.path.exists(self.vault))
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".claude", "projects")), "원천 기록은 지우지 않는다")

    def test_requirements_stop(self):
        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 2, out)
        self.assertIn("Claude Code 기록 폴더가 없다", out)
        self.assertFalse(os.path.exists(self.pwiki_home))
        self.fixture()
        rc, out = self.install("--yes", "--python", os.path.join(self.tmp, "없는-python"))
        self.assertEqual(rc, 2, out)
        self.assertIn("쓸 수 있는 python3 이 없다", out)
        rc, out = self.install("--bogus")
        self.assertEqual(rc, 2, out)

    def test_non_interactive_without_flags_skips_optional_steps(self):
        self.fixture()
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertIn("대화형이 아니라 건너뜀", out)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "settings.json")))


class HookInstallTest(DistBase):
    def write_settings(self, obj):
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        with open(self.settings, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
        os.chmod(self.settings, 0o644)
        return rd(self.settings, True)

    def test_hook_append_runs_and_uninstall_restores_bytes(self):
        info = self.fixture()
        orig = self.write_settings(OTHER_SETTINGS)
        rc, out = self.install("--hook", "--no-collector", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("쓴 뒤 검사: 통과", out)
        new = json.loads(rd(self.settings))
        for k in OTHER_SETTINGS:
            if k != "hooks":
                self.assertEqual(new[k], OTHER_SETTINGS[k])
        for ev in ("PreToolUse", "Stop", "SessionEnd"):
            self.assertEqual(new["hooks"][ev], OTHER_SETTINGS["hooks"][ev])
        ss = new["hooks"]["SessionStart"]
        self.assertEqual(len(ss), 2)
        self.assertEqual(ss[0], OTHER_SETTINGS["hooks"]["SessionStart"][0])
        cmd = ss[1]["hooks"][0]["command"]
        self.assertIn(os.path.join(os.path.realpath(ROOT), "install", "pwiki_session_start.py"), cmd)
        self.assertEqual(ss[1]["matcher"], "startup|clear")
        self.assertEqual(os.stat(self.settings).st_mode & 0o777, 0o644)
        # settings 에 적힌 명령 그대로 셸로 부른다(Claude Code 가 부르는 방식)
        rc, out = self.sh(["/bin/sh", "-c", cmd], stdin=json.dumps({"cwd": info["projects"]["beta"]["cwd"],
                                                                    "source": "clear"}))
        self.assertEqual(rc, 0, out)
        self.assertIn("beta-tools", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        once = rd(self.settings, True)
        rc, out = self.install("--hook", "--no-collector", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("이미 있다", out)
        self.assertEqual(rd(self.settings, True), once)
        rc, out = self.uninstall("--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(rd(self.settings, True), orig)
        self.assertTrue(os.path.isfile(os.path.join(self.pwiki_home, "pwiki.db")))
        rc, out = self.uninstall("--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("pwiki 항목이 없다", out)
        self.assertEqual(rd(self.settings, True), orig)

    def test_hook_dry_run_writes_nothing(self):
        self.fixture()
        orig = self.write_settings(OTHER_SETTINGS)
        rc, out = self.install("--hook", "--no-collector", "--yes", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("merge_settings.py", out)
        self.assertIn("--apply", out)
        self.assertEqual(rd(self.settings, True), orig)
        self.assertFalse(os.path.exists(self.pwiki_home))
        self.assertFalse(os.path.exists(self.vault))

    def test_refuses_second_pwiki_hook_from_other_location(self):
        self.fixture()
        other = json.loads(json.dumps(OTHER_SETTINGS))
        other["hooks"]["SessionStart"].append({"matcher": "startup|clear", "hooks": [
            {"type": "command", "command": "/usr/bin/python3 /opt/old/pwiki/install/pwiki_session_start.py", "timeout": 5}]})
        orig = self.write_settings(other)
        rc, out = self.install("--hook", "--no-collector", "--yes")
        self.assertEqual(rc, 3, out)
        self.assertIn("다른 명령의 pwiki 훅", out)
        self.assertEqual(rd(self.settings, True), orig)

    def test_missing_settings_is_created_then_hook_removed(self):
        self.fixture()
        settings = os.path.join(self.home, ".claude", "settings.json")
        rc, out = self.install("--hook", "--no-collector", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(json.loads(rd(settings))["hooks"]["SessionStart"]), 1)
        rc, out = self.uninstall("--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(rd(settings)), {})


class CollectorDryRunTest(DistBase):
    def test_collector_dry_run_prints_and_writes_nothing(self):
        self.fixture()
        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 0, out)
        rc, out = self.install("--collector", "--no-hook", "--yes", "--dry-run")
        self.assertEqual(rc, 0, out)
        plist = os.path.join(self.home, "Library", "LaunchAgents", "local.pwiki.collect.plist")
        if DARWIN:
            self.assertIn("bootstrap gui/%d %s" % (os.getuid(), plist), out)
            self.assertIn("+ install -m 644", out)
        else:
            self.assertIn("crontab", out)
            self.assertIn("pwiki_collect.py", out)
        self.assertFalse(os.path.exists(plist))
        self.assertFalse(os.path.exists(os.path.dirname(plist)))

    def render(self, repo, plist, old_comment=False):
        os.makedirs(os.path.dirname(plist), exist_ok=True)
        rc, out = self.sh([PY, RENDER, "--python", PY, "--repo", repo, "--pwiki-home", self.pwiki_home,
                           "--claude-dir", os.path.join(self.home, ".claude"), "--vault", self.vault, "--out", plist])
        self.assertEqual(rc, 0, out)
        if old_comment:  # 옛 plist 처럼 주석 안에 '--' 를 넣어 엄격한 파서가 못 읽게 한다
            text = rd(plist).replace("<dict>", "<!-- ingest --all -->\n<dict>", 1)
            with open(plist, "w", encoding="utf-8") as fh:
                fh.write(text)

    @unittest.skipUnless(DARWIN, "launchd 는 맥만")
    def test_uninstall_dry_run_collector(self):
        plist = os.path.join(self.home, "Library", "LaunchAgents", "local.pwiki.collect.plist")
        for old in (False, True):
            self.render(os.path.realpath(ROOT), plist, old_comment=old)
            rc, out = self.uninstall("--dry-run")
            self.assertEqual(rc, 0, out)
            self.assertIn("bootout gui/%d/local.pwiki.collect" % os.getuid(), out)
            self.assertIn("+ rm -f %s" % plist, out)
            self.assertTrue(os.path.exists(plist))

    @unittest.skipUnless(DARWIN, "launchd 는 맥만")
    def test_uninstall_leaves_plist_of_other_location(self):
        # 설치 기록 없이 돌려도 다른 저장소 자리의 수집기는 내리지 않는다(가짜 launchctl 이 불리면 tearDown 이 잡는다)
        plist = os.path.join(self.home, "Library", "LaunchAgents", "local.pwiki.collect.plist")
        for old in (False, True):
            self.render(os.path.join(self.tmp, "other-pwiki-src"), plist, old_comment=old)
            before = rd(plist, True)
            rc, out = self.uninstall()
            self.assertEqual(rc, 0, out)
            self.assertIn("다른 자리의 설치라 건드리지 않는다", out)
            self.assertEqual(rd(plist, True), before)


def copy_repo(dst):
    """저장소의 실행 파일만 dst 에 복사한다(다른 자리에 푼 저장소 흉내)."""
    os.makedirs(dst)
    for d in ("pwikilib", "install"):
        shutil.copytree(os.path.join(ROOT, d), os.path.join(dst, d), ignore=shutil.ignore_patterns("__pycache__"))
    for f in ("pwiki", "install.sh", "uninstall.sh"):
        shutil.copy2(os.path.join(ROOT, f), os.path.join(dst, f))
    return dst


class SafetyTest(DistBase):
    """검토에서 재현된 설치·제거 안전 결함의 회귀 시험."""

    def write_settings(self, obj):
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        with open(self.settings, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
        return rd(self.settings, True)

    def test_repo_at_default_vault_is_refused_and_purge_keeps_repo(self):
        self.fixture()
        repo = copy_repo(os.path.join(self.home, "pwiki"))
        rc, out = self.sh(["git", "init", "-q", repo])
        self.assertEqual(rc, 0, out)
        rc, out = self.sh(["/bin/bash", os.path.join(repo, "install.sh"), "--no-collector", "--no-hook", "--yes"])
        self.assertEqual(rc, 2, out)
        self.assertIn("pwiki 저장소", out)
        self.assertIn("겹친다", out)
        self.assertFalse(os.path.exists(self.pwiki_home))
        # 예전 설치가 남긴 것처럼 데이터 폴더와 _notes 가 있어도 저장소는 지우지 않는다
        os.makedirs(self.pwiki_home, mode=0o700)
        os.makedirs(os.path.join(repo, "_notes"))
        rc, out = self.sh(["/bin/bash", os.path.join(repo, "uninstall.sh"), "--purge-data", "--yes"])
        self.assertEqual(rc, 0, out)
        self.assertIn("지우지 않는다", out)
        self.assertTrue(os.path.isfile(os.path.join(repo, "install.sh")))
        self.assertTrue(os.path.isdir(os.path.join(repo, ".git")))
        self.assertFalse(os.path.exists(self.pwiki_home))

    def test_vault_with_user_notes(self):
        self.fixture()
        obs = os.path.join(self.home, "Obsidian")
        os.makedirs(os.path.join(obs, "내 노트"))
        with open(os.path.join(obs, "내 노트", "중요.md"), "w") as fh:
            fh.write("내 노트\n")
        rc, out = self.install("--no-collector", "--no-hook", "--yes", PWIKI_VAULT=obs)
        self.assertEqual(rc, 2, out)
        self.assertIn("pwiki 가 아닌 항목이 1개", out)
        self.assertFalse(os.path.exists(self.pwiki_home))
        # 숨김 파일만 있는 폴더(빈 Obsidian vault)는 받는다. 설치 뒤 사람이 넣은 파일은 purge 가 남긴다
        obs2 = os.path.join(self.home, "Obsidian2")
        os.makedirs(os.path.join(obs2, ".obsidian"))
        rc, out = self.install("--no-collector", "--no-hook", "--yes", PWIKI_VAULT=obs2)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isdir(os.path.join(obs2, "days")))
        os.makedirs(os.path.join(obs2, "내 노트"))
        for rel in ("내 노트/중요.md", "_notes/메모.md"):
            with open(os.path.join(obs2, rel), "w") as fh:
                fh.write("사람 메모\n")
        rc, out = self.uninstall("--purge-data", "--yes")  # vault 는 설치 기록에서 읽는다
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.pwiki_home))
        self.assertEqual(sorted(os.listdir(obs2)), [".obsidian", "_notes", "내 노트"])
        self.assertEqual(os.listdir(os.path.join(obs2, "_notes")), ["메모.md"])
        self.assertTrue(os.path.isfile(os.path.join(obs2, "내 노트", "중요.md")))

    def test_uninstall_without_install_record_removes_this_repo_hook(self):
        self.fixture()
        orig = self.write_settings(OTHER_SETTINGS)
        # (나) PWIKI_HOME 을 바꿔 설치하고 제거할 때 주지 않은 경우
        data = os.path.join(self.home, "data-pw")
        rc, out = self.install("--hook", "--no-collector", "--yes", PWIKI_HOME=data)
        self.assertEqual(rc, 0, out)
        self.assertIn("PWIKI_HOME=", rd(self.settings))
        rc, out = self.uninstall("--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("설치 기록: 없음", out)
        self.assertEqual(rd(self.settings, True), orig)
        # (가) 훅을 붙인 뒤 설치 기록을 쓰기 전에 멈춘 경우(--python 으로 다른 python 을 준 설치)
        wrap = os.path.join(self.tmp, "py-wrap")
        with open(wrap, "w") as fh:
            fh.write("#!/bin/sh\nexec %s \"$@\"\n" % PY)
        os.chmod(wrap, 0o700)
        rc, out = self.install("--hook", "--no-collector", "--yes", "--python", wrap)
        self.assertEqual(rc, 0, out)
        self.assertIn(wrap, rd(self.settings))
        self.assertEqual(rc, 0, out)
        os.unlink(os.path.join(self.pwiki_home, "install.json"))
        rc, out = self.uninstall("--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(rd(self.settings, True), orig)
        # 다른 자리의 pwiki 훅은 남긴다
        other = json.loads(json.dumps(OTHER_SETTINGS))
        other["hooks"]["SessionStart"].append({"matcher": "startup|clear", "hooks": [
            {"type": "command", "command": "/usr/bin/python3 /opt/old/pwiki/install/pwiki_session_start.py", "timeout": 5}]})
        orig2 = self.write_settings(other)
        rc, out = self.uninstall("--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("다른 자리의 pwiki 훅 1개는 그대로 둔다", out)
        self.assertEqual(rd(self.settings, True), orig2)

    def test_purge_refusal_changes_nothing(self):
        self.fixture()
        self.write_settings(OTHER_SETTINGS)
        rc, out = self.install("--hook", "--no-collector", "--yes")
        self.assertEqual(rc, 0, out)
        after = rd(self.settings, True)
        rc, out = self.uninstall("--purge-data")  # 대화형이 아니고 --yes 없음
        self.assertEqual(rc, 2, out)
        self.assertIn("아무것도 바꾸지 않았다", out)
        self.assertNotIn("## 1.", out)
        self.assertEqual(rd(self.settings, True), after)
        self.assertTrue(os.path.isfile(os.path.join(self.pwiki_home, "install.json")))

    def test_purge_guard_normalizes_home(self):
        self.fixture()
        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 0, out)
        for bad in (self.home + "//", self.home + "/./", os.path.dirname(self.home)):
            rc, out = self.uninstall("--purge-data", "--yes", "--dry-run", PWIKI_HOME=bad)
            self.assertEqual(rc, 0, out)
            self.assertIn("HOME 을 품은 폴더", out)
            targets = [ln[len("+ rm -rf "):].strip("'") for ln in out.splitlines() if ln.startswith("+ rm -rf ")]
            for t in targets:
                self.assertNotIn(os.path.realpath(t), (os.path.realpath(self.home), os.path.realpath(self.tmp)), out)

    def test_hangul_space_repo_alias_and_bare_python_name(self):
        self.fixture()
        repo = copy_repo(os.path.join(os.path.realpath(self.tmp), "한글 공백", "pwiki-src"))
        rc, out = self.sh(["/bin/bash", os.path.join(repo, "install.sh"), "--dry-run", "--no-collector", "--no-hook",
                           "--yes", "--python", "python3"], _cwd=self.home)
        self.assertEqual(rc, 0, out)
        py = shutil.which("python3", path=self.env()["PATH"])
        self.assertIn("+ %s '%s' ingest --all" % (py, os.path.join(repo, "pwiki")), out)
        line = [ln for ln in out.splitlines() if ln.startswith("줄여 쓰려면 셸 설정에: ")][0]
        alias = line.split(": ", 1)[1]
        self.assertNotIn("$'", alias)
        shells = [["/bin/bash", "-O", "expand_aliases", "-c"]]
        if os.path.exists("/bin/zsh"):
            shells.append(["/bin/zsh", "-f", "-c"])
        for shell in shells:
            # zsh 는 -c 글 전체를 먼저 읽으므로 별칭은 eval 로 실행 때 펼친다(셸 설정 파일에서는 줄마다 읽는다)
            rc, got = self.sh(shell + [alias + "\neval 'pwiki --help'\n"])
            self.assertEqual(rc, 0, (shell, got))
            self.assertIn("usage", got.lower())

    def test_trigram_missing_reason(self):
        self.fixture()
        sc = os.path.join(self.tmp, "sc")
        os.makedirs(sc)
        with open(os.path.join(sc, "sitecustomize.py"), "w") as fh:
            fh.write("import sqlite3\n_real = sqlite3.connect\n"
                     "class _C(object):\n"
                     "    def __init__(self, c):\n        self._c = c\n"
                     "    def execute(self, sql, *a):\n"
                     "        if 'trigram' in sql:\n            raise sqlite3.OperationalError('no such tokenizer')\n"
                     "        return self._c.execute(sql, *a)\n"
                     "sqlite3.connect = lambda *a, **k: _C(_real(*a, **k))\n")
        fake = os.path.join(self.tmp, "fake-python")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\nPYTHONPATH='%s' exec %s \"$@\"\n" % (sc, PY))
        os.chmod(fake, 0o700)
        rc, out = self.install("--yes", "--python", fake)
        self.assertEqual(rc, 2, out)
        self.assertIn("trigram 토크나이저가 없다", out)
        self.assertNotIn("3.34 이상 필요", out.split("python 3.9 이상")[0])
        self.assertFalse(os.path.exists(self.pwiki_home))


def _t(*parts):
    """검사기가 시험 파일 자체를 잡지 않게 조각을 실행할 때 잇는다."""
    return "".join(parts)


class LeakCheckTest(DistBase):
    def tree(self, files):
        root = os.path.join(self.tmp, "dist")
        for rel, text in files.items():
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
        shutil.copytree(os.path.join(ROOT, "pwikilib"), os.path.join(root, "pwikilib"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(ROOT, "tools"), os.path.join(root, "tools"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        return root

    def leak(self, root, *args):
        return self.sh([PY, LEAK, root] + list(args), PWIKI_HOME=os.path.join(self.tmp, "nohome"))

    def test_project_folder_name_is_caught(self):
        cdir = os.path.join(self.tmp, "claude")
        for n in ("-private-tmp-claude-zorbexrunner-meta-51234", "-Users-me-work", "-tmp-x"):
            os.makedirs(os.path.join(cdir, "projects", n))
        root = self.tree({"tests/fx.py": 'proj = "-private-tmp-claude-zorbexrunner-meta-777"\n'})
        rc, out = self.leak(root, "--claude-dir", cdir)
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"\(바\) 이 컴퓨터의 Claude Code 프로젝트 이름 \d+개: 걸림 1건")
        self.assertNotIn("zorbexrunner", out)
        shutil.rmtree(root)
        clean = self.tree({"tests/fx.py": 'proj = "-private-tmp-claude-batchjob-meta-777"\n'})
        rc, out = self.leak(clean, "--claude-dir", cdir)
        self.assertEqual(rc, 0, out)

    def test_clean_tree_passes_and_each_kind_is_caught(self):
        deny = os.path.join(self.tmp, "deny.txt")
        word = "Qorvexplank"
        with open(deny, "w") as fh:
            fh.write("# 시험\n%s\n" % word)
        clean = {"README.md": "서버 10.0.0.1 · 메일 someone@example.com · 키 %s\n" % _t("sk-ant-", "api03-", "abcdefghijklmn")}
        root = self.tree(clean)
        rc, out = self.leak(root, "--denylist", deny)
        self.assertEqual(rc, 0, out)
        self.assertIn("판정: 통과", out)
        shutil.rmtree(root)
        cases = {
            "denylist": "프로젝트 %s 메모\n" % word.lower(),
            "ip": "서버 %s\n" % _t("192.", "168.7.20"),
            "email": "연락 %s\n" % _t("kim.dev", "@", "corp-mail.co.kr"),
            "token": "키 %s\n" % _t("gh", "p_", "Q7mZk2LwX9rT4vNb8HcJ5yPd3sFg6aRe1uKx"),
            "home": "경로 %s/work\n" % self.home,  # 검사기는 HOME=self.home 으로 돈다
        }
        for name, text in cases.items():
            root = self.tree(dict(clean, **{"bad.txt": text}))
            rc, out = self.leak(root, "--denylist", deny)
            self.assertEqual(rc, 1, (name, out))
            self.assertIn("bad.txt", out)
            self.assertNotIn(text.split()[1], out, "값을 출력했다: " + name)
            shutil.rmtree(root)

    def test_make_dist_single_commit_and_leak_gate(self):
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        shutil.copytree(os.path.join(ROOT, "pwikilib"), os.path.join(src, "pwikilib"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(os.path.join(ROOT, "tools"), os.path.join(src, "tools"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        with open(os.path.join(src, "README.md"), "w") as fh:
            fh.write("깨끗한 문서\n")
        genv = dict(GIT_AUTHOR_NAME="someone", GIT_AUTHOR_EMAIL="someone@private.invalid",
                    GIT_COMMITTER_NAME="someone", GIT_COMMITTER_EMAIL="someone@private.invalid", GIT_CONFIG_NOSYSTEM="1")
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "a"]):
            rc, out = self.sh(["git", "-C", src] + args, **genv)
            self.assertEqual(rc, 0, out)
        with open(os.path.join(src, "README.md"), "a") as fh:
            fh.write("둘째\n")
        rc, out = self.sh(["git", "-C", src, "commit", "-qam", "b"], **genv)
        self.assertEqual(rc, 0, out)
        deny = os.path.join(self.tmp, "deny.txt")
        with open(deny, "w") as fh:
            fh.write("Qorvexplank\n")
        out_dir = os.path.join(self.tmp, "dist-out")
        email = "dist@example.invalid"
        rc, out = self.sh(["/bin/bash", os.path.join(src, "tools", "make_dist.sh"), "HEAD", out_dir],
                          PWIKI_DIST_EMAIL=email, PWIKI_DENYLIST=deny, PWIKI_HOME=os.path.join(self.tmp, "nohome"))
        self.assertEqual(rc, 0, out)
        self.assertIn("판정: 통과", out)
        rc, n = self.sh(["git", "-C", out_dir, "rev-list", "--count", "HEAD"])
        self.assertEqual(n.strip(), "1")
        rc, meta = self.sh(["git", "-C", out_dir, "log", "--format=%an|%ae|%cn|%ce"])
        self.assertEqual(meta.strip(), "pwiki|%s|pwiki|%s" % (email, email))
        self.assertEqual(rd(os.path.join(out_dir, "README.md")), "깨끗한 문서\n둘째\n")
        # 출력 폴더가 차 있으면 거절, 이메일·금지 목록이 없으면 거절
        rc, out = self.sh(["/bin/bash", os.path.join(src, "tools", "make_dist.sh"), "HEAD", out_dir],
                          PWIKI_DIST_EMAIL=email, PWIKI_DENYLIST=deny)
        self.assertEqual(rc, 2, out)
        rc, out = self.sh(["/bin/bash", os.path.join(src, "tools", "make_dist.sh"), "HEAD", out_dir + "2"],
                          PWIKI_DENYLIST=deny)
        self.assertEqual(rc, 2, out)
        # 금지 낱말이 든 커밋은 rc 1
        with open(os.path.join(src, "README.md"), "a") as fh:
            fh.write("qorvexplank\n")
        self.sh(["git", "-C", src, "commit", "-qam", "c"], **genv)
        rc, out = self.sh(["/bin/bash", os.path.join(src, "tools", "make_dist.sh"), "HEAD", out_dir + "3"],
                          PWIKI_DIST_EMAIL=email, PWIKI_DENYLIST=deny, PWIKI_HOME=os.path.join(self.tmp, "nohome"))
        self.assertEqual(rc, 1, out)
        self.assertIn("낱말 #1 · README.md:3", out)


if __name__ == "__main__":
    unittest.main()
