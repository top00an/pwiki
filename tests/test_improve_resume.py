"""개선 2·3(a): resume 재구성·검색 안내, 그리고 원문 내용을 읽지 않는다는 약속(수집 뒤 활동은 파일 이름·시각만).
가짜 비밀값만 쓴다."""
import datetime
import json
import os
import subprocess
import sys
import time
import unittest

from helpers import CWD, FAKE_SECRET, PROJ, Env, Sess, uid

from pwikilib import db as dbm
from pwikilib import ingest, views

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "install", "pwiki_session_start.py")
PY = sys.executable
FAKE_HARVEST = "Hv7fakeHarvest9Q2xLm"  # 비밀번호 문맥으로만 나오는 가짜 값(수확 대상)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def stamp(lines, start):
    """줄 시각을 start(UTC datetime)부터 1초씩 다시 매긴다."""
    for i, o in enumerate(lines):
        if isinstance(o, dict) and "timestamp" in o:
            o["timestamp"] = _iso(start + datetime.timedelta(seconds=i))
    return lines


def now_utc():
    return datetime.datetime.utcnow().replace(microsecond=0)


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.log = os.path.join(self.env.tmp, "hook.log")

    def tearDown(self):
        self.env.close()

    def hook(self, data, extra=None):
        e = {"HOME": "/Users/tester", "PATH": "/usr/bin:/bin", "PWIKI_HOME": self.env.home,
             "PWIKI_CLAUDE_DIR": self.env.claude, "PWIKI_HOOK_LOG": self.log}
        e.update(extra or {})
        d = dict({"cwd": CWD, "source": "startup", "hook_event_name": "SessionStart", "session_id": uid()}, **data)
        p = subprocess.run([PY, HOOK], input=json.dumps(d).encode("utf-8"), capture_output=True, env=e, timeout=10)
        self.assertEqual(p.returncode, 0)
        if not p.stdout:
            return ""
        return json.loads(p.stdout.decode("utf-8"))["hookSpecificOutput"]["additionalContext"]

    def write(self, s, lines):
        self.env.write_lines(self.env.session_path(s.sid), lines)


class TestNoRawContent(Base):
    """요청·답은 DB(수집 때 가린 값)에서만 낸다. 수집 뒤에 원문에 더해진 줄은 내용을 싣지 않고,
    그 세션이 수집 뒤 활동했다는 것(세션 ID 앞 8자·시각)만 알린다."""

    def _stale(self):
        s = Sess()
        t = now_utc() - datetime.timedelta(minutes=20)
        self.write(s, stamp([s.human("옛 요청 디비"), s.assistant_text("옛 답 디비")], t))
        ingest.run()
        # 수집 뒤에 쓴 줄(아직 DB 에 없음): 알려진 가짜 비밀값과 비밀번호 문맥 값이 든다
        more = stamp([s.human("새 요청 꼬리 %s 그리고 password=%s 끝" % (FAKE_SECRET, FAKE_HARVEST)),
                      s.assistant_text("새 답 꼬리 %s" % FAKE_SECRET)], t + datetime.timedelta(minutes=10))
        self.write(s, more)
        return s

    def test_uncollected_lines_are_not_shown(self):
        s = self._stale()
        out = self.hook({})
        self.assertIn("옛 요청 디비", out)
        self.assertIn("옛 답 디비", out)
        for x in ("새 요청 꼬리", "새 답 꼬리", FAKE_SECRET, FAKE_HARVEST, FAKE_SECRET[3:13]):
            self.assertNotIn(x, out)
        self.assertNotIn("원문 꼬리 반영", out)
        self.assertLessEqual(len(out), 4000)
        # 수집 뒤 활동 한 줄: 세션 ID 앞 8자만, 그리고 원문 내용을 읽지 않았다는 표시
        line = [ln for ln in out.splitlines() if ln.startswith("마지막 수집(")]
        self.assertEqual(len(line), 1, out[:600])
        self.assertIn(s.sid[:8], line[0])
        self.assertNotIn(s.sid, line[0])
        self.assertIn("원문 내용은 읽지 않음", line[0])

    def test_pending_line_lists_new_session_and_skips_self(self):
        s = self._stale()
        n, me = Sess(), Sess()
        self.write(n, stamp([n.human("새 세션 요청 미수집")], now_utc() - datetime.timedelta(minutes=1)))
        self.write(me, stamp([me.human("지금 여는 세션")], now_utc()))
        out = self.hook({"session_id": me.sid})
        line = [ln for ln in out.splitlines() if ln.startswith("마지막 수집(")][0]
        self.assertIn(s.sid[:8], line)
        self.assertIn(n.sid[:8], line)
        self.assertNotIn(me.sid[:8], line)
        self.assertNotIn("새 세션 요청 미수집", out)
        # 수집하면 줄이 사라지고 DB 값이 그대로 나온다
        ingest.run()
        out2 = self.hook({"session_id": me.sid})
        self.assertFalse([ln for ln in out2.splitlines() if ln.startswith("마지막 수집(")], out2[:600])
        self.assertIn("새 요청 꼬리", out2)
        self.assertNotIn(FAKE_SECRET, out2)
        self.assertNotIn(FAKE_HARVEST, out2)

    def test_pending_line_ignores_mtime_only_change(self):
        """크기는 그대로이고 시각만 바뀐 파일은 수집이 못 담은 활동이 아니다. 수집기는 크기가 같으면 건너뛰어
        files.mtime 을 고치지 않으므로, 시각만 보면 수집 앞의 시각을 '수집 뒤 활동'으로 잘못 알린다."""
        s = Sess()
        p = self.env.session_path(s.sid)
        self.write(s, stamp([s.human("크기 그대로 요청"), s.assistant_text("크기 그대로 답")],
                            now_utc() - datetime.timedelta(minutes=30)))
        t0 = time.time()
        os.utime(p, (t0 - 600, t0 - 600))
        ingest.run()
        size = os.path.getsize(p)
        # (1) 수집 뒤 시각만 앞으로 옮기고 다시 수집: 그 시각은 마지막 수집보다 앞이다
        os.utime(p, (t0 - 300, t0 - 300))
        ingest.run()
        out = self.hook({})
        self.assertIn("크기 그대로 요청", out)
        self.assertFalse([ln for ln in out.splitlines() if ln.startswith("마지막 수집(")], out[:600])
        # (2) 마지막 수집 뒤에 시각만 바뀜(내용은 그대로 DB 에 있음)
        t1 = time.time() + 1
        os.utime(p, (t1, t1))
        self.assertEqual(os.path.getsize(p), size)
        out2 = self.hook({})
        self.assertFalse([ln for ln in out2.splitlines() if ln.startswith("마지막 수집(")], out2[:600])
        # (3) 대조: 같은 파일에 줄이 늘면 알린다
        self.write(s, stamp([s.human("늘어난 요청 미수집")], now_utc()))
        out3 = self.hook({})
        line = [ln for ln in out3.splitlines() if ln.startswith("마지막 수집(")]
        self.assertEqual(len(line), 1, out3[:600])
        self.assertIn(s.sid[:8], line[0])
        self.assertNotIn("늘어난 요청 미수집", out3)

    def test_raw_session_files_never_opened(self):
        """훅 build 를 같은 프로세스에서 돌리며 기록 폴더(~/.claude) 아래 파일 열기를 모두 막는다.
        stat·이름 목록은 되고 open 은 안 된다. 막힌 열기가 한 번이라도 있으면 실패다."""
        import builtins
        import importlib.util
        import io
        s = self._stale()
        spec = importlib.util.spec_from_file_location("hookmod_noraw", HOOK)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        root = os.path.realpath(self.env.claude)
        hits = []

        def under(p):
            try:
                rp = os.path.realpath(os.fsdecode(p)) if isinstance(p, (str, bytes, os.PathLike)) else ""
            except Exception:
                return False
            return rp == root or rp.startswith(root + os.sep)

        real_open, real_io_open, real_os_open = builtins.open, io.open, os.open

        def g_open(f, *a, **k):
            if under(f):
                hits.append("open")
                raise PermissionError("blocked")
            return real_open(f, *a, **k)

        def g_os_open(f, *a, **k):
            if under(f):
                hits.append("os.open")
                raise PermissionError("blocked")
            return real_os_open(f, *a, **k)
        builtins.open, io.open, os.open = g_open, g_open, g_os_open
        try:
            text, outcome = m.build(CWD, source="startup", session_id=uid())
        finally:
            builtins.open, io.open, os.open = real_open, real_io_open, real_os_open
        self.assertEqual(outcome, "ok")
        self.assertEqual(hits, [])
        self.assertIn("옛 요청 디비", text)
        self.assertIn(s.sid[:8], text)
        self.assertNotIn("새 요청 꼬리", text)

    def test_no_tail_module(self):
        """원문 꼬리 읽기 모듈은 없다(되살아나면 이 시험이 알린다)."""
        self.assertFalse(os.path.exists(os.path.join(ROOT, "pwikilib", "live.py")))
        with open(HOOK, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("live_tails", src)
        self.assertNotIn("PWIKI_HOOK_TAIL_BUDGET", src)


class TestResumeShape(Base):
    def test_parallel_sessions_all_listed_and_search_hint(self):
        t = now_utc() - datetime.timedelta(minutes=90)
        a, b = Sess(), Sess()
        self.write(a, stamp([a.human("에이 세션 병렬 요청"), a.assistant_text("에이 답")], t))
        self.write(b, stamp([b.human("비 세션 최신 요청"), b.assistant_text("비 답")], t + datetime.timedelta(minutes=30)))
        ingest.run()
        out = self.hook({})
        self.assertIn(b.sid, out)
        self.assertIn(a.sid[:8], out)
        self.assertIn("에이 세션 병렬 요청", out)
        self.assertIn("pwiki search", out)
        self.assertIn("pwiki show", out)
        self.assertLessEqual(len(out), 4000)

    def test_clear_keeps_other_sessions_and_drops_self(self):
        """/clear 여도 원문을 읽지 않으므로 다른 세션은 빼지 않는다. 새 세션 자신(session_id)만 뺀다."""
        t = now_utc() - datetime.timedelta(minutes=45)
        a, b = Sess(), Sess()
        self.write(b, stamp([b.human("비 세션 예전 요청"), b.assistant_text("비 답")], t))
        self.write(a, stamp([a.human("에이 방금 지운 요청"), a.assistant_text("에이 지운 답")], t + datetime.timedelta(minutes=40)))
        ingest.run()
        out = self.hook({"source": "clear"})
        self.assertIn("에이 방금 지운 요청", out)
        self.assertIn("비 세션 예전 요청", out)
        out2 = self.hook({"source": "startup", "session_id": a.sid})
        self.assertNotIn("에이 방금 지운 요청", out2)
        self.assertNotIn(a.sid[:8], out2)
        self.assertIn("비 세션 예전 요청", out2)

    def test_error_section_skips_harness_and_old(self):
        s = Sess()
        tu, tuid = s.tool_use("Bash", {"command": "make build"})
        tu2, tuid2 = s.tool_use("Bash", {"command": "sleep 30"})
        lines = [s.human("빌드해"), tu, s.tool_result(tuid, "Exit code 2\nmake: *** [build] Error 1", is_error=True),
                 tu2, s.tool_result(tuid2, "Blocked: sleep 30 followed by: echo. Permission to use Bash has been denied.",
                                    is_error=True)]
        t = now_utc() - datetime.timedelta(minutes=30)
        self.write(s, stamp(lines, t))
        ingest.run()
        con = dbm.open_db(readonly=True)
        now = _iso(now_utc())
        out = views.resume(con, PROJ, now=now)
        self.assertIn("make build", out.split("## 해결 안 된 마지막 에러")[1])
        self.assertNotIn("sleep 30", out.split("## 해결 안 된 마지막 에러")[1].split("\n## ")[0])
        later = _iso(now_utc() + datetime.timedelta(hours=30))
        out2 = views.resume(con, PROJ, now=later)
        con.close()
        self.assertIn("## 해결 안 된 마지막 에러\n- 없음", out2)

    def test_truncation_mark_survives_line_cap(self):
        s = Sess()
        req = "\n".join("줄 %02d 내용" % i for i in range(30))
        self.write(s, stamp([s.human(req), s.assistant_text("답")], now_utc() - datetime.timedelta(minutes=10)))
        ingest.run()
        con = dbm.open_db(readonly=True)
        out = views.resume(con, PROJ)
        con.close()
        sec = out.split("## 마지막 사람 요청")[1].split("\n## ")[0]
        self.assertIn("잘림", sec)
        self.assertNotIn("줄 29 내용", sec)


if __name__ == "__main__":
    unittest.main()
