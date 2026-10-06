"""install/skill.py: 이 저장소가 쓴 스킬만 쓰고 지운다. 링크·이상한 경로·남의 파일을 건드리지 않는다."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest

from helpers import ROOT

spec = importlib.util.spec_from_file_location("pwiki_skill", os.path.join(ROOT, "install", "skill.py"))
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)

PY = "/usr/bin/python3"


def quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        rc = fn(*a)
    return rc, out.getvalue()


class SkillHelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="skill_", dir=os.environ.get("PWIKI_TEST_TMP") or None)
        self.d = os.path.join(self.tmp, "skills", "pwiki")
        self.path = os.path.join(self.d, "SKILL.md")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_repo_path_with_spaces_round_trips(self):
        repo = os.path.join(self.tmp, "My Projects old", "pwiki")
        self.assertEqual(quiet(skill.install, self.d, repo, PY, False)[0], 0)
        self.assertEqual(skill.owner(self.path), repo)
        rc, out = quiet(skill.install, self.d, repo, PY, False)
        self.assertEqual(rc, 0)
        self.assertIn("스킬 같음", out)
        # 앞부분만 같은 다른 저장소는 지우지 못한다
        self.assertEqual(quiet(skill.remove, self.d, os.path.join(self.tmp, "My Projects"), False)[0], 3)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(quiet(skill.remove, self.d, repo, False)[0], 0)
        self.assertFalse(os.path.lexists(self.path))

    def test_old_marker_is_still_recognised(self):
        os.makedirs(self.d)
        repo = "/Users/x/pwiki-src"
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("---\nname: pwiki\n---\n<!-- pwiki-skill repo=%s (written by pwiki install.sh; ./uninstall.sh removes it) -->\n" % repo)
        self.assertEqual(skill.owner(self.path), repo)

    def test_planted_tmp_symlink_is_not_followed(self):
        os.makedirs(self.d)
        victim = os.path.join(self.tmp, "victim.txt")
        with open(victim, "w") as fh:
            fh.write("keep me")
        os.chmod(victim, 0o600)
        os.symlink(victim, os.path.join(self.d, "SKILL.md.pwiki-tmp"))
        self.assertEqual(quiet(skill.install, self.d, "/opt/pwiki", PY, False)[0], 0)
        with open(victim) as fh:
            self.assertEqual(fh.read(), "keep me")
        self.assertEqual(os.stat(victim).st_mode & 0o777, 0o600)
        self.assertFalse(os.path.islink(self.path))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)
        leftovers = [f for f in os.listdir(self.d) if f.startswith(".SKILL.md.")]
        self.assertEqual(leftovers, [])

    def test_broken_symlink_and_human_symlink_are_left_alone(self):
        os.makedirs(self.d)
        os.symlink(os.path.join(self.tmp, "missing.md"), self.path)
        rc, out = quiet(skill.install, self.d, "/opt/pwiki", PY, False)
        self.assertEqual(rc, 3)
        self.assertIn("끊긴 링크", out)
        self.assertTrue(os.path.islink(self.path))
        self.assertEqual(quiet(skill.remove, self.d, "/opt/pwiki", False)[0], 3)
        self.assertTrue(os.path.islink(self.path))

    def test_paths_that_break_skill_syntax_are_refused(self):
        for repo in ("/a/b: c/pwiki", "/a/b #x/pwiki", "/a/b,c/pwiki", "/a/(b)/pwiki", '/a/"b"/pwiki', "/a/b-->c/pwiki"):
            rc, out = quiet(skill.install, self.d, repo, PY, False)
            self.assertEqual(rc, 3, repo)
            self.assertFalse(os.path.lexists(self.path), repo)

    def test_allowed_tools_lists_lookup_commands_only(self):
        quiet(skill.install, self.d, "/opt/my pwiki", PY, False)
        with open(self.path, encoding="utf-8") as fh:
            head = fh.read().split("---")[1]
        line = [l for l in head.splitlines() if l.startswith("allowed-tools:")][0]
        cmd = "/usr/bin/python3 '/opt/my pwiki/pwiki'"
        for sub in ("today)", "day *)", "search *)", "show *)", "resume *)", "redact-check)", "ingest --all)"):
            self.assertIn("Bash(%s %s" % (cmd, sub), line)
        for bad in ("rederive", "export", "%s *)" % cmd):
            self.assertNotIn(bad, line)
        self.assertEqual(json.loads(json.dumps(skill.owner(self.path))), "/opt/my pwiki")


if __name__ == "__main__":
    unittest.main()
