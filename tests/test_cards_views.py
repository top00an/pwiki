import json
import os
import unittest

from helpers import PROJ, Env, Sess, uid
from pwikilib import db as dbm
from pwikilib import export as exportm
from pwikilib import ingest, views


class TestCards(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_usage_counted_once_per_message_id(self):
        """assistant 응답 하나가 여러 줄로 나뉘고 usage 가 줄마다 반복돼도 message.id 당 한 번만 센다."""
        s = Sess()
        u1 = {"input_tokens": 5, "output_tokens": 40, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 9000}
        u2 = dict(u1, output_tokens=120)
        lines = [s.human("요청")]
        a, t = s.tool_use("Bash", {"command": "ls"}, mid="msg_same", usage=u1)
        lines += [s.assistant_text("생각 정리", mid="msg_same", usage=u1), a, s.assistant_text("끝", mid="msg_same", usage=u2),
                  s.tool_result(t, "ok")]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        ingest.run()
        con = dbm.open_db()
        c = con.execute("SELECT n_asst, tok_in, tok_out, tok_cc, tok_cr, n_tools FROM cards").fetchone()
        self.assertEqual(c, (1, 5, 120, 1000, 9000, 1))
        con.close()

    def test_queued_dedup_and_boundaries(self):
        s = Sess()
        lines = [s.human("첫 요청")]
        o, t = s.tool_use("Edit", {"file_path": "/w/a.py", "old_string": "a", "new_string": "b"})
        lines += [o]
        # 작업 도중 입력: enqueue → remove → queued_command 첨부(같은 글)
        lines += [s.plain("queue-operation", operation="enqueue", timestamp="2026-09-30T01:00:03.000Z", content="이것도 고쳐"),
                  s.plain("queue-operation", operation="remove", timestamp="2026-09-30T01:00:04.000Z", content="이것도 고쳐"),
                  s.tool_result(t, "ok"),
                  s.attachment({"type": "queued_command", "prompt": "이것도 고쳐", "commandMode": "prompt",
                                "origin": {"kind": "human"}, "source_uuid": uid()})]
        o2, t2 = s.tool_use("Bash", {"command": "pytest -q"})
        lines += [o2, s.tool_result(t2, "Exit code 1\nFAILED test_x.py::t - assert 1 == 2", is_error=True)]
        o3, t3 = s.tool_use("Bash", {"command": "pytest -q"})
        lines += [o3, s.tool_result(t3, "Exit code 1\nFAILED test_x.py::t - assert 3 == 4", is_error=True)]
        # 전달되지 않은 대기열 입력(짝 없음)
        lines += [s.plain("queue-operation", operation="enqueue", timestamp="2026-09-30T01:00:30.000Z", content="취소한 입력"),
                  s.plain("queue-operation", operation="remove", timestamp="2026-09-30T01:00:31.000Z")]
        lines += [s.base("user", message={"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]})]
        o4, t4 = s.tool_use("Bash", {"command": "rm -rf x"})
        lines += [o4, s.tool_result(t4, "The user doesn't want to proceed with this tool use.", is_error=True,
                                    toolDenialKind="user-rejected")]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        ingest.run()
        con = dbm.open_db()
        cards = con.execute("SELECT key, human, human_kind, delivered, n_tools, n_files, n_err, n_err_repeat, n_denied, n_intr,"
                            " files, cmds FROM cards ORDER BY seq").fetchall()
        self.assertEqual(len(cards), 3)
        c1, c2, c3 = cards
        self.assertEqual(c1[1], "첫 요청")
        self.assertEqual(c1[5], 1)
        self.assertEqual(json.loads(c1[10]), ["/w/a.py"])
        self.assertEqual(c2[1], "이것도 고쳐")
        self.assertEqual(c2[2], "queued")
        att_uuid = lines[5]["uuid"]
        self.assertEqual(c2[0], "c:%s:%s" % (s.sid, att_uuid))
        self.assertEqual(c2[6], 3)
        self.assertEqual(c2[7], 1, "같은 에러 서명 반복 1")
        self.assertEqual(c2[8], 1)
        self.assertEqual(c2[9], 1)
        self.assertEqual(c3[1], "취소한 입력")
        self.assertEqual(c3[3], 0)
        self.assertTrue(c3[0].startswith("c:%s:o:" % s.sid))
        con.close()

    def build_multi(self):
        s = Sess()
        lines = []
        for i in range(12):
            lines.append(s.human("요청 %d 번: %s" % (i, "긴 설명 " * 30)))
            o, t = s.tool_use("TodoWrite", {"todos": [{"content": "할 일 A%d" % i, "status": "completed"},
                                                      {"content": "할 일 B%d" % i, "status": "in_progress"}]})
            lines += [o, s.tool_result(t, "ok")]
            o, t = s.tool_use("Bash", {"command": "make build%d" % i})
            lines += [o, s.tool_result(t, "Exit code 2\nmake: *** no rule", is_error=True)]
            lines.append(s.assistant_text("답 %d: %s" % (i, "내용 " * 50)))
        lines.append(s.plain("ai-title", aiTitle="여러 요청 세션"))
        self.env.write_lines(self.env.session_path(s.sid), lines)
        ingest.run()
        return s

    def test_resume_cap_and_content(self):
        s = self.build_multi()
        con = dbm.open_db(readonly=True)
        proj = views.resolve_project(con, "/Users/tester/work/demo")
        self.assertEqual(proj, PROJ)
        out = views.resume(con, proj, 4000, now="2026-09-30T02:00:00.000Z")  # 에러 절은 24시간 안만
        self.assertLessEqual(len(out), 4000)
        self.assertIn("## 마지막 세션", out)
        self.assertIn(s.sid, out)
        self.assertIn("할 일 B11", out)
        self.assertNotIn("할 일 A11", out)
        self.assertIn("해결 안 된 마지막 에러", out)
        self.assertIn("make build11", out)
        small = views.resume(con, proj, 900)
        self.assertLessEqual(len(small), 900)
        self.assertIn("잘림", small)
        con.close()

    def test_day_and_eff(self):
        self.build_multi()
        con = dbm.open_db(readonly=True)
        d = views.day(con, "2026-09-30")
        self.assertIn("사람 입력 12건 = 카드 12장 + 인자 없는 슬래시 명령 0건", d)
        self.assertIn("demo", d)
        e = views.eff(con)
        self.assertIn("TodoWrite 사용: 예 | 12", e)
        self.assertIn("| 에러 |", e)
        self.assertNotIn("추천", e)
        con.close()

    def test_search_token_fallback(self):
        """낱말 OR 결합. 3글자 이상은 FTS OR, 3글자 미만은 LIKE 후보·가점. 순위는 일치 낱말 수 → bm25."""
        s = Sess()
        lines = [s.human("커서 역행 결함을 찾아 줘 unable to open database file")]
        lines.append(s.assistant_text("원인은 WAL 모드에서 읽기 전용 연결이었다"))
        self.env.write_lines(self.env.session_path(s.sid), lines)
        ingest.run()
        con = dbm.open_db(readonly=True)
        r = views.search(con, "unable to open database")
        self.assertEqual(r["method"], "fts-or+like-or")
        self.assertTrue(r["rows"])
        self.assertEqual(r["rows"][0]["matched"], 4)
        r = views.search(con, "unable open database")
        self.assertEqual(r["method"], "fts-or")
        self.assertTrue(r["rows"])
        r = views.search(con, "결함")
        self.assertEqual(r["method"], "like-or")
        self.assertTrue(r["rows"])
        r = views.search(con, "커서 역행")
        self.assertEqual(r["method"], "like-or")
        self.assertTrue(any(x["key"].startswith(s.sid) or x["key"].startswith("c:") for x in r["rows"]))
        r = views.search(con, "연결이었다 WAL")
        self.assertTrue(r["rows"])
        # OR: 없는 낱말이 섞여도 있는 낱말로 걸리고, 일치 낱말 수가 드러난다
        r = views.search(con, "없는낱말조합 결함")
        self.assertTrue(r["rows"])
        self.assertEqual(r["rows"][0]["matched"], 1)
        self.assertEqual(views.search(con, "없는낱말조합 또없는말")["rows"], [])
        r = views.search(con, "결함", kind="card")
        self.assertTrue(all(x["kind"] == "card" for x in r["rows"]) and r["rows"])
        con.close()

    def test_search_or_ranking_and_korean_stem(self):
        """문장형 질의: 낱말 일부만 든 줄도 걸리고, 더 많이 든 줄이 먼저다. 한국어 조사·어미를 뗀 어간도 센다."""
        s = Sess()
        lines = [s.human("로그 정리"), s.assistant_text("예시 원천의 커서는 sample_col 열로 확정했고 역행은 0회였다"),
                 s.human("다른 일"), s.assistant_text("커서 이야기만 한 줄"),
                 s.human("세 번째"), s.assistant_text("전혀 관계없는 답")]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        ingest.run()
        con = dbm.open_db(readonly=True)
        r = views.search(con, "커서가 역행하는 문제를 어떻게 확정했지", kind="assistant")
        self.assertTrue(r["rows"])
        self.assertIn("sample_col", r["rows"][0]["snippet"])
        self.assertGreater(r["rows"][0]["matched"], r["rows"][1]["matched"])
        self.assertEqual(views.ko_stem("커서가"), "커서")
        self.assertEqual(views.ko_stem("역행하는"), "역행")
        self.assertIsNone(views.ko_stem("커서"))
        con.close()

    def test_export_conflict(self):
        self.build_multi()
        con = dbm.open_db()
        r1 = exportm.export(con)
        self.assertGreater(r1["counts"].get("created", 0), 0)
        self.assertEqual(r1["n_conflicts"], 0)
        r2 = exportm.export(con)
        self.assertEqual(r2["counts"].get("created", 0), 0)
        self.assertEqual(r2["counts"].get("unchanged"), r1["pages"])
        day = os.path.join(self.env.vault, "days", "2026-09-30.md")
        with open(day, "a", encoding="utf-8") as fh:
            fh.write("\n사람이 덧붙인 메모\n")
        r3 = exportm.export(con)
        self.assertEqual(r3["n_conflicts"], 1)
        with open(day, encoding="utf-8") as fh:
            self.assertIn("사람이 덧붙인 메모", fh.read())
        self.assertTrue(os.path.exists(os.path.join(self.env.vault, "_notes", "README.md")))
        with open(day, encoding="utf-8") as fh:
            txt = fh.read()
        self.assertIn("다시 읽지 않으며", txt)
        self.assertIn("[[", txt)
        self.assertTrue(txt.startswith("---\n"))
        con.close()


if __name__ == "__main__":
    unittest.main()


class TestCardEdges(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_end_time_ignores_away_summary_and_resume_picks_latest_human(self):
        a = Sess(t0="2026-09-30T01:00:00")
        la = [a.human("A 세션 요청"), a.assistant_text("A 답")]
        away = a.system("away_summary", content="자리 비운 동안 요약")
        away["timestamp"] = "2026-09-30T09:00:00.000Z"
        la.append(away)
        b = Sess(t0="2026-09-30T02:00:00")
        lb = [b.human("B 세션 요청"), b.assistant_text("B 답")]
        self.env.write_lines(self.env.session_path(a.sid), la)
        self.env.write_lines(self.env.session_path(b.sid), lb)
        ingest.run()
        con = dbm.open_db(readonly=True)
        dur = con.execute("SELECT dur_s FROM cards WHERE sid=?", (a.sid,)).fetchone()[0]
        self.assertLess(dur, 60, "자리 비움 요약 줄은 소요에 넣지 않는다")
        out = views.resume(con, PROJ)
        self.assertIn(b.sid, out.split("## 마지막 사람 요청")[0])
        self.assertIn("B 세션 요청", out)
        con.close()
