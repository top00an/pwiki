"""수정 1회차에서 남은 것: 검색 점수, 훅 실패 안전·안내 줄."""
import datetime
import os
import unittest

from helpers import Sess

from pwikilib import db as dbm
from pwikilib import ingest, views
from test_improve_resume import Base, now_utc, stamp

FILL = int(os.environ.get("FIX1_FILL") or 60)


class TestHookSafety(Base):
    def _one(self):
        s = Sess()
        self.write(s, stamp([s.human("하나 요청"), s.assistant_text("하나 답")], now_utc() - datetime.timedelta(minutes=5)))
        ingest.run()

    def test_bad_deadline_env(self):
        self._one()
        out = self.hook({}, extra={"PWIKI_HOOK_DEADLINE": "abc"})
        self.assertIn("하나 요청", out)

    def test_hint_points_to_this_install(self):
        """안내 줄은 이 저장소의 pwiki 경로를 쓴다(설치 자리는 사람마다 다르다)."""
        import shlex
        self._one()
        out = self.hook({})
        cmd = shlex.quote(os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "pwiki"))
        self.assertIn(cmd + " search", out)
        self.assertIn(cmd + " show", out)
        self.assertNotIn("pwiki-src", out.replace(cmd, ""))
        self.assertNotIn("원문 꼬리 반영", out)


class TestSearchScore(Base):
    def test_full_match_beats_dense_partial(self):
        """낱말 셋을 다 맞춘 행이 한 낱말만 짧고 촘촘하게 맞춘 행보다 앞이다."""
        s = Sess()
        lines = []
        for i in range(12):
            lines += [s.human("잡담 %d" % i), s.assistant_text("관계없는 답 %d 번째 줄" % i)]
        filler = " ".join("채움말%03d" % i for i in range(FILL))
        lines += [s.human("부분"), s.assistant_text("zebrafish")]
        lines += [s.human("전부"), s.assistant_text("zebrafish quokka narwhal " + filler)]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        ingest.run()
        con = dbm.open_db(readonly=True)
        r = views.search(con, "zebrafish quokka narwhal", kind="assistant", per_session=0)
        self.assertEqual(r["rows"][0]["matched"], 3)
        con.close()


if __name__ == "__main__":
    unittest.main()
