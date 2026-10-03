"""독립 검증 2차 지적(2026-10-01) 재현 시험. 가짜 값만 쓴다."""
import os
import tempfile
import unittest

from helpers import FAKE_SECRET, Env, Sess, uid
from pwikilib import check, ingest, parse, views
from pwikilib import db as dbm
from pwikilib import leakscan
from pwikilib.redact import Checker, Redactor


def _bare(s, text, **kw):
    """감싸개 없이 문자열로 적힌 슬래시 명령 user 줄(실측 모양: promptId 는 있고 origin·promptSource 는 없다)."""
    return s.base("user", message={"role": "user", "content": text}, promptId=uid(), **kw)


def _u(ch, upper=False):
    h = "%04x" % ord(ch)
    return "\\u" + (h.upper() if upper else h)


class TestBareSlash(unittest.TestCase):
    def test_classify(self):
        s = Sess()
        self.assertEqual(parse.classify(_bare(s, "/compact")), ("command", None))
        self.assertEqual(parse.classify(_bare(s, "  /compact\n")), ("command", None))
        self.assertEqual(parse.classify(_bare(s, "/compact 요약은 짧게")), ("slash", "/compact"))
        self.assertEqual(parse.classify(_bare(s, "/plugin:do-it")), ("command", None))
        # 경로·origin 이 있는 줄은 슬래시 명령이 아니다
        self.assertEqual(parse.classify(_bare(s, "/Users/x/a.txt 봐"))[0], "user_other")
        self.assertEqual(parse.classify(_bare(s, "/compact", origin={"kind": "task-notification"}))[0], "task_note")
        self.assertEqual(parse.classify(_bare(s, "/compact", isSidechain=True))[0], "side_user")


class EnvCase(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()


class TestBareSlashCounted(EnvCase):
    def test_day_and_history_crosscheck(self):
        s = Sess()
        lines = [s.human("첫 요청"), s.assistant_text("했다"), _bare(s, "/compact"),
                 s.system("compact_boundary", content="Conversation compacted")]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        hist = os.path.join(self.env.claude, "history.jsonl")
        self.env.write_lines(hist, [{"display": "첫 요청", "pastedContents": {}, "timestamp": 1790000000000,
                                     "project": "/Users/tester/work/demo", "sessionId": s.sid},
                                    {"display": "/compact", "pastedContents": {}, "timestamp": 1790000001000,
                                     "project": "/Users/tester/work/demo", "sessionId": s.sid}])
        ingest.run()
        con = dbm.open_db()
        self.assertEqual(con.execute("SELECT count(*) FROM events WHERE kind='command'").fetchone()[0], 1)
        d = views.day(con, "2026-09-30")
        self.assertIn("사람 입력 2건 = 카드 1장 + 인자 없는 슬래시 명령 1건", d)
        h = check.history_crosscheck(con)["totals"]
        self.assertEqual(h.get("slash_counted"), 1)
        self.assertEqual(h.get("slash_uncounted", 0), 0)
        self.assertEqual(h.get("matched"), 1)
        # 감시가 동작하는지: 옛 분류(user_other)로 되돌리면 '줄은 있는데 안 셈' 으로 드러난다
        con.execute("UPDATE events SET kind='user_other' WHERE kind='command'")
        h = check.history_crosscheck(con)
        self.assertEqual(h["totals"].get("slash_uncounted"), 1)
        self.assertTrue(any("/compact" in e[1] for e in h["unmatched_examples"]))
        con.close()


class TestUnicodeEscapeVariant(unittest.TestCase):
    """알려진 값의 특수기호를 \\uXXXX 로 적은 변형(일부 글자만, 16진 대소문자, JSON 두 겹)."""

    def variants(self):
        v = FAKE_SECRET  # 특수기호 # 와 $ 가 든 가짜 값
        allu = "".join(_u(c) if not c.isalnum() else c for c in v)
        one = v.replace("#", _u("#", upper=True))
        dbl = allu.replace("\\", "\\\\")
        return [allu, one, dbl, "echo '%s!' > x" % allu]

    def test_redactor_and_checker(self):
        R = Redactor([FAKE_SECRET])
        C = Checker([FAKE_SECRET])
        for s in self.variants():
            d = {}
            C.count_text(s, d)
            self.assertEqual(d.get("known"), 1, s)
            r = R.text(s)
            self.assertIn("[REDACTED:known]", r, s)
            d = {}
            C.count_text(r, d)
            self.assertEqual(d, {}, s)

    def test_independent_scan_counts_variants(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "x.bin")
            with open(p, "wb") as fh:
                fh.write(("앞 %s 중간 %s 끝 " % tuple(self.variants()[:2])).encode("utf-8"))
                fh.write(("{\"t\":\"%s\"} " % self.variants()[2]).encode("utf-8"))
                fh.write(("평문 %s 셸 %s" % (FAKE_SECRET, FAKE_SECRET.replace("#", "\\#").replace("$", "\\$"))).encode("utf-8"))
            self.assertEqual(leakscan.count_in_file(p, [FAKE_SECRET]), {FAKE_SECRET: 5})
            # 다른 값과 이어진 글은 세지 않는다(닻만 같고 나머지가 다름)
            with open(p, "wb") as fh:
                fh.write(FAKE_SECRET.replace("#", "\\u0040").encode("utf-8"))
            self.assertEqual(leakscan.count_in_file(p, [FAKE_SECRET]), {})


if __name__ == "__main__":
    unittest.main()
