"""수정 2회차에서 남은 것: -wal 없는 DB 읽기 전용 열기, 마감 환경변수 (0, 5] 자르기."""
import datetime
import json
import os
import subprocess
import unittest

from helpers import CWD, Sess, uid

from pwikilib import ingest, paths
from test_improve_resume import HOOK, PY, Base, now_utc, stamp


class TestReadonlyNoWal(Base):
    def test_hook_and_cli_without_wal(self):
        s = Sess()
        self.write(s, stamp([s.human("왈 없는 요청"), s.assistant_text("왈 없는 답")], now_utc() - datetime.timedelta(minutes=5)))
        ingest.run()
        db = paths.db_path()
        for x in ("-wal", "-shm"):
            if os.path.exists(db + x):
                os.remove(db + x)
        self.assertFalse(os.path.exists(db + "-wal"))
        with open(db, "rb") as fh:
            before = fh.read()
        out = self.hook({})
        self.assertIn("왈 없는 요청", out)
        e = dict(os.environ, PWIKI_HOME=self.env.home, PWIKI_CLAUDE_DIR=self.env.claude)
        repo = os.path.dirname(os.path.dirname(HOOK))
        p = subprocess.run([PY, os.path.join(repo, "pwiki"), "search", "없는"], capture_output=True, env=e, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace")[-300:])
        self.assertNotIn(b"Traceback", p.stderr)
        with open(db, "rb") as fh:
            self.assertEqual(fh.read(), before)


class TestDeadlineClamp(Base):
    def test_extreme_values(self):
        s = Sess()
        self.write(s, stamp([s.human("마감 요청"), s.assistant_text("마감 답")], now_utc() - datetime.timedelta(minutes=5)))
        ingest.run()
        e0 = {"HOME": "/Users/tester", "PATH": "/usr/bin:/bin", "PWIKI_HOME": self.env.home,
              "PWIKI_CLAUDE_DIR": self.env.claude, "PWIKI_HOOK_LOG": "-"}
        d = json.dumps({"cwd": CWD, "source": "startup", "session_id": uid()}).encode()
        for v in ("inf", "1e400", "-inf", "-1", "0", "nan", "abc", " ", "9", "0.0000001"):
            p = subprocess.run([PY, HOOK], input=d, capture_output=True, env=dict(e0, PWIKI_HOOK_DEADLINE=v), timeout=10)
            self.assertEqual(p.returncode, 0, v)
            self.assertEqual(p.stderr, b"", v)
            if v != "0.0000001":
                self.assertIn("마감 요청", p.stdout.decode("utf-8"), v)

    def test_clamp_function(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("hookmod", HOOK)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        old = os.environ.get("PWIKI_HOOK_DEADLINE")
        try:
            for v, want in (("inf", 5.0), ("1e400", 5.0), ("9", 5.0), ("-1", 1.8), ("nan", 1.8), ("abc", 1.8),
                            ("2.5", 2.5)):
                os.environ["PWIKI_HOOK_DEADLINE"] = v
                self.assertEqual(m._deadline_s(), want, v)
        finally:
            if old is None:
                os.environ.pop("PWIKI_HOOK_DEADLINE", None)
            else:
                os.environ["PWIKI_HOOK_DEADLINE"] = old


if __name__ == "__main__":
    unittest.main()
