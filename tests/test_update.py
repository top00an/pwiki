"""pwiki update: 임시 원격(bare)과 설치 사본(clone)으로 실제 git 동작을 본다. 네트워크·실제 설치는 쓰지 않는다."""
import os
import shutil
import subprocess
import tempfile
import unittest

from helpers import Env
from pwikilib import update

CL = """# Changelog

## [0.3.0] - 2026-10-20
- 셋째

## [0.2.0] - 2026-10-06
- 둘째

## [0.1.0] - 2026-10-04
- 첫째
"""


def git(cwd, *args):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com", HOME=cwd)
    return subprocess.run(["git", "-C", cwd, "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"] + list(args),
                          check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                          env=env).stdout


@unittest.skipUnless(shutil.which("git"), "git 없음")
class TestUpdate(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.tmp = tempfile.mkdtemp(prefix="pwiki-upd-")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.dev = os.path.join(self.tmp, "dev")
        self.inst = os.path.join(self.tmp, "inst")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.remote], check=True)
        os.makedirs(os.path.join(self.dev, "pwikilib"))
        subprocess.run(["git", "init", "-q", "-b", "main", self.dev], check=True)
        self.commit("0.1.0", "## [0.1.0] - 2026-10-04\n- 첫째\n")
        git(self.dev, "remote", "add", "origin", self.remote)
        git(self.dev, "push", "-q", "origin", "main")
        subprocess.run(["git", "clone", "-q", self.remote, self.inst], check=True)
        self.old_repo = update.REPO
        update.REPO = self.inst
        self.lines = []

    def tearDown(self):
        update.REPO = self.old_repo
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.env.close()

    def commit(self, ver, cl):
        with open(os.path.join(self.dev, "pwikilib", "__init__.py"), "w") as fh:
            fh.write('VERSION = "%s"\n' % ver)
        with open(os.path.join(self.dev, "CHANGELOG.md"), "w") as fh:
            fh.write(cl)
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", ver)

    def run_update(self, check_only=False):
        self.lines = []
        return update.run(check_only=check_only, out=self.lines.append)

    def head(self):
        return git(self.inst, "rev-parse", "HEAD").strip()

    def test_up_to_date(self):
        self.assertEqual(self.run_update(), 0)
        self.assertTrue(any(ln.startswith("최신입니다") for ln in self.lines), self.lines)

    def test_check_changes_nothing_then_update(self):
        self.commit("0.2.0", CL)
        git(self.dev, "push", "-q", "origin", "main")
        h0 = self.head()
        self.run_update(check_only=True)
        self.assertEqual(self.head(), h0)
        self.assertTrue(any("0.2.0" in ln and "새 판" in ln for ln in self.lines), self.lines)
        self.run_update()
        self.assertNotEqual(self.head(), h0)
        out = "\n".join(self.lines)
        self.assertIn("설치를 다시 돌리지 않았다", out)  # 이 시험 환경엔 install.json 이 없다
        self.assertIn("- 둘째", out)
        self.assertNotIn("- 셋째", out)  # 받은 판(0.2.0)보다 뒤 절은 싣지 않는다

    def test_local_edit_is_not_overwritten(self):
        self.commit("0.2.0", CL)
        git(self.dev, "push", "-q", "origin", "main")
        with open(os.path.join(self.inst, "CHANGELOG.md"), "a") as fh:
            fh.write("내가 고침\n")
        h0 = self.head()
        with self.assertRaises(update.UpdateError) as cm:
            self.run_update()
        self.assertIn("CHANGELOG.md", str(cm.exception))
        self.assertEqual(self.head(), h0)
        with open(os.path.join(self.inst, "CHANGELOG.md")) as fh:
            self.assertIn("내가 고침", fh.read())

    def test_record_without_repo_does_not_run_install(self):
        self.commit("0.2.0", CL)
        git(self.dev, "push", "-q", "origin", "main")
        with open(os.path.join(self.inst, "install.sh"), "w") as fh:
            fh.write("#!/bin/bash\ntouch \"$(dirname \"$0\")/RAN\"\n")
        from pwikilib import paths
        with open(os.path.join(paths.pwiki_home(), "install.json"), "w") as fh:
            fh.write('{"collector": true, "hook": false, "skill": false}')
        old = os.getcwd()
        os.chdir(self.inst)  # 빈 repo 값이 작업 폴더로 풀려 '같은 폴더'로 판정되던 경로
        try:
            self.run_update()
        finally:
            os.chdir(old)
        self.assertFalse(os.path.exists(os.path.join(self.inst, "RAN")))
        self.assertIn("설치를 다시 돌리지 않았다", "\n".join(self.lines))

    def test_not_a_git_clone(self):
        update.REPO = self.tmp
        with self.assertRaises(update.UpdateError):
            self.run_update()


class TestUpdatePure(unittest.TestCase):
    def test_install_args_keep_off_choices(self):
        a = update.install_args({"collector": False, "hook": True, "skill": False, "python": "/usr/bin/python3",
                                 "settings": "/x/settings.json"})
        self.assertEqual(a, ["--no-collector", "--hook", "--no-skill", "--python", "/usr/bin/python3",
                             "--settings", "/x/settings.json"])
        self.assertEqual(update.install_args({}), ["--no-collector", "--no-hook", "--no-skill"])
        # 스킬만 켜도 settings 위치를 넘긴다(스킬 자리가 거기서 정해진다)
        self.assertIn("--settings", update.install_args({"skill": True, "settings": "/x/settings.json"}))

    def test_changelog_between(self):
        s = update.changelog_between(CL, "0.1.0", "0.3.0")
        self.assertIn("셋째", s)
        self.assertIn("둘째", s)
        self.assertNotIn("첫째", s)


if __name__ == "__main__":
    unittest.main()
