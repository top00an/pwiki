"""2단계 설치물 시험(설치하지 않는다): settings 합치기, SessionStart 훅, 수집 감싸개, launchd plist 템플릿.
실제 ~/.claude·~/.pwiki·launchd 는 건드리지 않는다. 임시 HOME 과 임시 PWIKI_HOME 에서만 돈다."""
import difflib
import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import CWD, ROOT, Env, Sess

INSTALL = os.path.join(ROOT, "install")
PY = "/usr/bin/python3"
MERGE = os.path.join(INSTALL, "merge_settings.py")
HOOK = os.path.join(INSTALL, "pwiki_session_start.py")
COLLECT = os.path.join(INSTALL, "pwiki_collect.py")
PLIST_IN = os.path.join(INSTALL, "local.pwiki.collect.plist.in")
RENDER = os.path.join(INSTALL, "render_plist.py")
MARK = "pwiki-test-marker-not-secret"

FAKE_SETTINGS = {
    "env": {"PWIKI_TEST_MARK": MARK},
    "permissions": {"allow": ["Bash(ls:*)"], "deny": []},
    "hooks": {
        "SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "/opt/other-app/start.sh " + MARK}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/guard/check.py", "timeout": 10}]}],
        "Stop": [{"hooks": [{"type": "command", "command": "node /opt/app/hook.js stop", "async": True}]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": "/opt/review/한글 경로.sh"}]},
                       {"hooks": [{"type": "command", "command": "echo 끝"}]}],
    },
    "statusLine": {"type": "command", "command": "/opt/status.sh"},
    "enabledPlugins": {"a@b": True},
    "cleanupPeriodDays": 30,
}


def rd(path, binary=False):
    with open(path, "rb" if binary else "r", **({} if binary else {"encoding": "utf-8"})) as fh:
        return fh.read()


def wjson(path, obj, **kw):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, **kw)


def load_merge():
    spec = importlib.util.spec_from_file_location("pwiki_merge_settings", MERGE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def canon(x):
    return json.dumps(x, ensure_ascii=False)


class MergeSettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pwiki-merge-")
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        # 조각은 명령으로 만든다(설치기가 설치 경로·python 경로로 만드는 것과 같은 함수)
        sn = load_merge().make_snippet("%s %s" % (PY, HOOK))
        self.group = sn["hooks"]["SessionStart"][0]
        self.snippet = os.path.join(self.tmp, "snippet.json")
        wjson(self.snippet, sn, indent=2)
        self.write(FAKE_SETTINGS)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, obj, text=None, mode=0o644):
        with open(self.settings, "w", encoding="utf-8") as fh:
            fh.write(text if text is not None else json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
        os.chmod(self.settings, mode)

    def run_merge(self, *args):
        env = {"HOME": self.home, "PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"}
        p = subprocess.run([PY, MERGE, "--snippet", self.snippet] + list(args), env=env, capture_output=True,
                           timeout=30)
        return p.returncode, p.stdout.decode("utf-8") + p.stderr.decode("utf-8")

    def raw(self):
        return rd(self.settings, True)

    def test_preview_writes_nothing(self):
        before, st = self.raw(), os.stat(self.settings)
        rc, out = self.run_merge()
        self.assertEqual(rc, 0, out)
        self.assertIn("미리보기", out)
        self.assertEqual(self.raw(), before)
        self.assertEqual(os.stat(self.settings).st_mtime_ns, st.st_mtime_ns)

    def test_apply_appends_one_and_keeps_existing(self):
        rc, out = self.run_merge("--apply")
        self.assertEqual(rc, 0, out)
        new = json.loads(self.raw())
        for k in FAKE_SETTINGS:
            if k != "hooks":
                self.assertEqual(canon(new[k]), canon(FAKE_SETTINGS[k]))
        self.assertEqual(list(new.keys()), list(FAKE_SETTINGS.keys()))
        self.assertEqual(list(new["hooks"].keys()), list(FAKE_SETTINGS["hooks"].keys()))
        for ev in ("PreToolUse", "Stop", "SessionEnd"):
            self.assertEqual(canon(new["hooks"][ev]), canon(FAKE_SETTINGS["hooks"][ev]))
        ss = new["hooks"]["SessionStart"]
        self.assertEqual(len(ss), 2)
        self.assertEqual(canon(ss[0]), canon(FAKE_SETTINGS["hooks"]["SessionStart"][0]))
        self.assertEqual(canon(ss[1]), canon(self.group))
        self.assertEqual(os.stat(self.settings).st_mode & 0o777, 0o644)
        self.assertIn("쓴 뒤 검사: 통과", out)

    def test_text_diff_is_only_the_added_group(self):
        before = self.raw().decode("utf-8").splitlines()
        self.run_merge("--apply")
        after = self.raw().decode("utf-8").splitlines()
        diff = [d for d in difflib.unified_diff(before, after, lineterm="", n=0) if not d.startswith(("---", "+++", "@@"))]
        minus = [d[1:] for d in diff if d.startswith("-")]
        plus = [d[1:] for d in diff if d.startswith("+")]
        # 바뀌는 기존 줄은 많아야 앞 그룹을 닫는 '}' 에 쉼표가 붙는 한 줄이다
        self.assertLessEqual(len(minus), 1, diff)
        for m in minus:
            self.assertEqual(m.strip(), "}")
            self.assertIn(m + ",", plus)
        # 기존 줄은 순서대로 모두 남는다(쉼표 한 개 차이만 허용)
        it = iter(x.rstrip(",") for x in after)
        self.assertTrue(all(any(b.rstrip(",") == y for y in it) for b in before))
        added = [x for x in plus if x.strip() not in ("}", "},", "]", "],", "{", "[")]
        self.assertEqual(len(added), 5, added)  # matcher·hooks·type·command·timeout
        self.assertTrue(any("pwiki_session_start.py" in x for x in added))

    def test_apply_twice_is_noop(self):
        self.run_merge("--apply")
        once = self.raw()
        rc, out = self.run_merge("--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("이미 있다", out)
        self.assertEqual(self.raw(), once)

    def test_remove_restores_original_bytes(self):
        orig = self.raw()
        self.run_merge("--apply")
        self.assertNotEqual(self.raw(), orig)
        rc, out = self.run_merge("--remove")
        self.assertEqual(rc, 0, out)
        self.assertNotEqual(self.raw(), orig)  # 미리보기는 쓰지 않는다
        rc, out = self.run_merge("--remove", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.raw(), orig)
        rc, out = self.run_merge("--remove", "--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("pwiki 항목이 없다", out)

    def test_no_session_start_and_no_hooks(self):
        for obj in ({"hooks": {"Stop": FAKE_SETTINGS["hooks"]["Stop"]}, "x": 1}, {"x": 1, "y": [1.0, True]}):
            self.write(obj)
            orig = self.raw()
            rc, out = self.run_merge("--apply")
            self.assertEqual(rc, 0, out)
            new = json.loads(self.raw())
            self.assertEqual(canon(new["hooks"]["SessionStart"]), canon([self.group]))
            self.assertEqual(new["x"], 1)
            rc, out = self.run_merge("--remove", "--apply")
            self.assertEqual(rc, 0, out)
            self.assertEqual(self.raw(), orig)

    def test_refuses_invalid_inputs_without_writing(self):
        cases = [
            ('{"hooks": {"SessionStart": [}', "JSON 이 아니다"),
            ('{"a": 1, "a": 2}', "중복 키"),
            ('[1, 2]', "객체가 아니다"),
            ('{"hooks": {"SessionStart": {"x": 1}}}', "배열이 아니다"),
            ('{"hooks": {"Stop": [{"matcher": "x"}]}}', "hooks 배열이 없는"),
            ('{"hooks": []}', "hooks 가 객체가 아니다"),
        ]
        for text, why in cases:
            self.write(None, text=text)
            before = self.raw()
            rc, out = self.run_merge("--apply")
            self.assertEqual(rc, 3, (text, out))
            self.assertIn(why, out)
            self.assertEqual(self.raw(), before)

    def test_refuses_missing_hook_file_and_missing_settings(self):
        sn = json.loads(rd(self.snippet))
        sn["hooks"]["SessionStart"][0]["hooks"][0]["command"] = PY + " " + os.path.join(self.tmp, "없는.py")
        wjson(self.snippet, sn)
        before = self.raw()
        rc, out = self.run_merge("--apply")
        self.assertEqual(rc, 3, out)
        self.assertIn("파일이 없다", out)
        self.assertEqual(self.raw(), before)
        os.unlink(self.settings)
        good = os.path.join(self.tmp, "good.json")
        wjson(good, load_merge().make_snippet("%s %s" % (PY, HOOK)))
        rc, out = self.run_merge("--apply", "--snippet", good)
        self.assertEqual(rc, 3)
        self.assertFalse(os.path.exists(self.settings))

    def test_symlink_writes_target_and_keeps_link(self):
        real = os.path.join(self.tmp, "dotfiles", "settings.json")
        os.makedirs(os.path.dirname(real))
        shutil.move(self.settings, real)
        os.symlink(real, self.settings)
        rc, out = self.run_merge("--apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.islink(self.settings))
        self.assertEqual(len(json.loads(rd(real))["hooks"]["SessionStart"]), 2)

    def test_concurrent_write_aborts(self):
        m = load_merge()
        real_dump = m.dump
        other = b'{"changed": "by-other-writer"}\n'

        def dump_and_race(obj, fmt):
            with open(self.settings, "wb") as fh:
                fh.write(other)
            return real_dump(obj, fmt)
        m.dump = dump_and_race
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = m.main(["--settings", self.settings, "--snippet", self.snippet, "--apply"])
        self.assertEqual(rc, 4, buf.getvalue())
        self.assertEqual(self.raw(), other)
        self.assertEqual([f for f in os.listdir(os.path.dirname(self.settings)) if f.endswith(".tmp")], [])

    def test_output_never_echoes_existing_values(self):
        for args in ((), ("--apply",), ("--remove",), ("--remove", "--apply")):
            rc, out = self.run_merge(*args)
            self.assertEqual(rc, 0, out)
            for s in (MARK, "/opt/guard/check.py", "Bash(ls:*)", "/opt/status.sh"):
                self.assertNotIn(s, out)


def build_db(env, n_humans=3, long=False):
    from pwikilib import ingest
    s = Sess()
    lines = []
    for i in range(n_humans):
        lines.append(s.human(("이어 하기 시험 요청 %d " % i) + ("긴 설명 " * 60 if long else "")))
        lines.append(s.assistant_text("응답 %d" % i))
    env.write_lines(env.session_path(s.sid), lines)
    ingest.run()
    return s


def sha1(p):
    return hashlib.sha1(rd(p, True)).hexdigest()


class SessionStartHookTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.log = os.path.join(self.env.tmp, "hook.log")

    def tearDown(self):
        self.env.close()

    def run_hook(self, stdin, extra=None):
        e = {"HOME": "/Users/tester", "PATH": "/usr/bin:/bin", "PWIKI_HOME": self.env.home, "PWIKI_HOOK_LOG": self.log}
        e.update(extra or {})
        t = time.time()
        p = subprocess.run([PY, HOOK], input=stdin.encode("utf-8"), capture_output=True, env=e, timeout=10)
        return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8"), time.time() - t

    def last_log(self):
        return json.loads(rd(self.log).splitlines()[-1])

    def test_outputs_resume_for_cwd_under_limit(self):
        build_db(self.env)
        db = os.path.join(self.env.home, "pwiki.db")
        before = sha1(db)
        rc, out, err, dt = self.run_hook(json.dumps({"cwd": CWD, "source": "startup", "hook_event_name": "SessionStart"}))
        self.assertEqual((rc, err), (0, ""))
        self.assertLess(dt, 2.0)
        d = json.loads(out)
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "SessionStart")
        text = d["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(text), 4000)
        self.assertTrue(text.startswith("[pwiki 이어 하기]"))
        self.assertIn("# 이어 하기", text)
        self.assertIn("이어 하기 시험 요청 2", text)
        self.assertEqual(sha1(db), before)
        self.assertEqual(self.last_log()["outcome"], "ok")
        self.assertEqual(self.last_log()["source"], "startup")

    def test_subfolder_resolves_to_project_and_long_is_capped(self):
        build_db(self.env, n_humans=60, long=True)
        rc, out, err, dt = self.run_hook(json.dumps({"cwd": CWD + "/sub/dir", "source": "clear"}))
        self.assertEqual(rc, 0)
        text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(text), 4000)
        self.assertGreater(len(text), 2000)

    def test_quiet_cases_exit_zero_without_output(self):
        # DB 없음: 만들지도 않는다
        rc, out, err, _ = self.run_hook(json.dumps({"cwd": CWD}))
        self.assertEqual((rc, out, err), (0, "", ""))
        self.assertFalse(os.path.exists(os.path.join(self.env.home, "pwiki.db")))
        self.assertEqual(self.last_log()["outcome"], "no_db")
        build_db(self.env)
        for stdin, extra, why in (
                (json.dumps({"cwd": "/nowhere/else"}), None, "no_project"),
                ("이건 JSON 이 아니다", None, "error:JSONDecodeError"),
                ("[1,2]", {"PWIKI_HOME": os.path.join(self.env.tmp, "없음")}, "no_db"),
                (json.dumps({"cwd": CWD}), {"PWIKI_EXTRACT": "1"}, "skip_extract")):
            rc, out, err, dt = self.run_hook(stdin, extra)
            self.assertEqual((rc, out, err), (0, "", ""), why)
            self.assertEqual(self.last_log()["outcome"], why)
            self.assertLess(dt, 2.0)

    def test_deadline_exits_zero_fast(self):
        build_db(self.env)
        # 표준 입력을 닫지 않으면 읽기에서 막힌다 → 마감 타이머가 0 으로 끝내야 한다
        e = {"HOME": "/Users/tester", "PATH": "/usr/bin:/bin", "PWIKI_HOME": self.env.home, "PWIKI_HOOK_LOG": self.log,
             "PWIKI_HOOK_DEADLINE": "0.3"}
        t = time.time()
        p = subprocess.Popen([PY, HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=e)
        rc = p.wait(timeout=10)
        dt = time.time() - t
        out = p.stdout.read()
        for f in (p.stdin, p.stdout, p.stderr):
            f.close()
        self.assertEqual((rc, out), (0, b""))
        self.assertLess(dt, 1.5)
        self.assertEqual(self.last_log()["outcome"], "deadline")


class CollectWrapperTest(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.logs = os.path.join(self.env.home, "logs")
        s = Sess()
        self.sess = s
        self.path = self.env.session_path(s.sid)
        self.env.write_lines(self.path, [s.human("수집 시험 하나"), s.assistant_text("응답")])

    def tearDown(self):
        self.env.close()

    def run_collect(self, extra=None):
        # launchd 처럼 최소 환경으로 띄운다
        e = {"HOME": self.env.tmp, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PWIKI_HOME": self.env.home,
             "PWIKI_CLAUDE_DIR": self.env.claude, "PWIKI_VAULT": self.env.vault}
        e.update(extra or {})
        p = subprocess.run([PY, COLLECT], env=e, capture_output=True, timeout=120)
        return p.returncode, p.stdout.decode("utf-8") + p.stderr.decode("utf-8")

    def status(self):
        return json.loads(rd(os.path.join(self.logs, "collect.status.json")))

    def log_lines(self):
        return [json.loads(x) for x in rd(os.path.join(self.logs, "collect.log")).splitlines()]

    def n_events(self):
        con = sqlite3.connect(os.path.join(self.env.home, "pwiki.db"))
        try:
            return con.execute("SELECT count(*) FROM events").fetchone()[0]
        finally:
            con.close()

    def test_ok_run_records_and_private_files(self):
        rc, out = self.run_collect()
        self.assertEqual(rc, 0, out)
        st = self.status()
        self.assertEqual((st["last_outcome"], st["last_rc"], st["fails_in_row"]), ("ok", 0, 0))
        self.assertIn("last_ok_at", st)
        self.assertEqual(len(self.log_lines()), 1)
        last = rd(os.path.join(self.logs, "collect.last.txt"))
        self.assertIn("# ingest", last)
        for f in ("collect.log", "collect.status.json", "collect.last.txt"):
            self.assertEqual(os.stat(os.path.join(self.logs, f)).st_mode & 0o777, 0o600, f)
        self.assertGreater(self.n_events(), 0)

    def test_lock_held_means_busy_and_no_ingest(self):
        self.run_collect()
        n0 = self.n_events()
        self.env.write_lines(self.path, [self.sess.human("잠금 중에 온 새 줄"), self.sess.assistant_text("응답 둘")])
        fd = os.open(os.path.join(self.env.home, "ingest.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            rc, out = self.run_collect()
            self.assertEqual(rc, 0, out)
            self.assertEqual(self.status()["last_outcome"], "busy")
            self.assertEqual(self.log_lines()[-1]["rc"], 75)
            self.assertEqual(self.n_events(), n0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        rc, out = self.run_collect()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.status()["last_outcome"], "ok")
        self.assertGreater(self.n_events(), n0)

    def fake_bin(self, body):
        p = os.path.join(self.env.tmp, "fake-pwiki")
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(p, 0o700)
        return p

    def test_failure_counts_then_next_run_recovers(self):
        bad = self.fake_bin("echo 일부러 실패; exit 1")
        for i in (1, 2):
            rc, out = self.run_collect({"PWIKI_BIN": bad})
            self.assertEqual(rc, 1, out)
            st = self.status()
            self.assertEqual((st["last_outcome"], st["fails_in_row"]), ("fail", i))
            self.assertNotIn("last_ok_at", st)
        rc, out = self.run_collect({"PWIKI_BIN": self.fake_bin("exit 2")})
        self.assertEqual((rc, self.status()["last_outcome"], self.status()["fails_in_row"]), (2, "mismatch", 3))
        rc, out = self.run_collect()
        self.assertEqual(rc, 0, out)
        st = self.status()
        self.assertEqual((st["last_outcome"], st["fails_in_row"]), ("ok", 0))
        self.assertEqual([x["outcome"] for x in self.log_lines()], ["fail", "fail", "mismatch", "ok"])

    def test_timeout_kills_process_group(self):
        pidf = os.path.join(self.env.tmp, "child.pid")
        slow = self.fake_bin("sleep 60 & echo $! > %s; wait" % pidf)
        t = time.time()
        rc, out = self.run_collect({"PWIKI_BIN": slow, "PWIKI_COLLECT_TIMEOUT": "1"})
        self.assertLess(time.time() - t, 40)
        self.assertEqual(rc, 124, out)
        self.assertEqual(self.status()["last_outcome"], "timeout")
        child = int(rd(pidf).strip())
        time.sleep(0.2)
        with self.assertRaises(OSError):
            os.kill(child, 0)

    def test_log_rotates(self):
        os.makedirs(self.logs, exist_ok=True)
        with open(os.path.join(self.logs, "collect.log"), "w") as fh:
            fh.write("x" * 2100000 + "\n")
        self.run_collect()
        self.assertTrue(os.path.exists(os.path.join(self.logs, "collect.log.1")))
        self.assertEqual(len(self.log_lines()), 1)


class PlistTest(unittest.TestCase):
    def test_template_renders_lint_and_keys(self):
        tmp = tempfile.mkdtemp(prefix="pwiki-plist-")
        try:
            repo = os.path.join(tmp, "설치 & 폴더")
            vals = {"PYTHON": PY, "REPO": repo, "PWIKI_HOME": os.path.join(tmp, "h", ".pwiki"),
                    "CLAUDE_DIR": os.path.join(tmp, "h", ".claude"), "VAULT": os.path.join(tmp, "h", "pwiki")}
            out = os.path.join(tmp, "out.plist")
            p = subprocess.run([PY, RENDER, "--python", PY, "--repo", repo, "--pwiki-home", vals["PWIKI_HOME"],
                                "--claude-dir", vals["CLAUDE_DIR"], "--vault", vals["VAULT"], "--out", out],
                               capture_output=True)
            self.assertEqual(p.returncode, 0, p.stderr)
            if os.path.exists("/usr/bin/plutil"):
                p = subprocess.run(["/usr/bin/plutil", "-lint", out], capture_output=True)
                self.assertEqual(p.returncode, 0, p.stdout)
            import plistlib
            with open(out, "rb") as fh:
                d = plistlib.load(fh)
            self.assertEqual(d["Label"], "local.pwiki.collect")
            self.assertEqual(d["StartInterval"], 1800)
            self.assertTrue(d["RunAtLoad"])
            self.assertEqual(d["ProgramArguments"], [PY, os.path.join(repo, "install", "pwiki_collect.py")])
            self.assertEqual(d["WorkingDirectory"], repo)
            self.assertIn("/usr/bin", d["EnvironmentVariables"]["PATH"])
            self.assertEqual(d["EnvironmentVariables"]["PWIKI_HOME"], vals["PWIKI_HOME"])
            self.assertEqual(d["EnvironmentVariables"]["PWIKI_CLAUDE_DIR"], vals["CLAUDE_DIR"])
            self.assertEqual(d["EnvironmentVariables"]["PWIKI_VAULT"], vals["VAULT"])
            self.assertEqual(d["StandardOutPath"], os.path.join(vals["PWIKI_HOME"], "logs", "collect.launchd.log"))
            # 템플릿에는 사용자 경로가 없다(자리표시만)
            self.assertNotIn(os.path.expanduser("~"), rd(PLIST_IN))
            # 상대 경로는 거절한다
            p = subprocess.run([PY, RENDER, "--python", PY, "--repo", "rel", "--pwiki-home", "/a", "--claude-dir", "/b",
                                "--vault", "/c"], capture_output=True)
            self.assertEqual(p.returncode, 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_default_hook_command_points_at_this_repo(self):
        m = load_merge()
        import shlex
        self.assertEqual(m.make_command("/usr/bin/python3"), "/usr/bin/python3 " + shlex.quote(HOOK))
        cmd = m.make_command(PY, os.path.join(tempfile.gettempdir(), "다른 홈", ".pwiki"))
        self.assertTrue(cmd.startswith("/usr/bin/env 'PWIKI_HOME="), cmd)
        self.assertTrue(cmd.endswith("pwiki_session_start.py") or cmd.endswith("pwiki_session_start.py'"), cmd)
        self.assertNotIn("PWIKI_HOME", m.make_command(PY, os.path.join(os.path.expanduser("~"), ".pwiki")))


if __name__ == "__main__":
    unittest.main()
