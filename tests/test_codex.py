"""Codex CLI 기록 수집: 합성 rollout·history 만 쓴다(실제 ~/.codex 는 읽지 않는다)."""
import json
import os
import unittest

from helpers import FAKE_SECRET, PROJ, CodexSess, Env, Sess
from pwikilib import check, ingest, views
from pwikilib import db as dbm


class TestCodex(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def write(self, s, lines, mode="a"):
        self.env.write_lines(s.path(self.env), lines, mode=mode)

    def test_ingest_cards_and_accounting(self):
        s = CodexSess()
        self.write(s, s.basic())
        a = ingest.run()
        self.assertEqual(a["accounting"]["mismatch_files"], 0)
        ex = a["excluded"]
        self.assertEqual(ex.get("codex:echo:user_message"), 1)
        self.assertEqual(ex.get("codex:injected:environment_context"), 1)
        self.assertEqual(ex.get("codex:reasoning"), 2)
        self.assertEqual(ex.get("codex:token_count"), 1)
        con = dbm.open_db()
        cards = con.execute("SELECT human, project FROM cards WHERE sid=?", (s.sid,)).fetchall()
        self.assertEqual(cards, [("코덱스로 테스트 고쳐줘", PROJ)])
        self.assertEqual(con.execute("SELECT src FROM sessions WHERE sid=?", (s.sid,)).fetchone()[0], "codex")
        self.assertEqual(con.execute("SELECT DISTINCT src FROM files WHERE sid=?", (s.sid,)).fetchall(), [("codex",)])
        tu = con.execute("SELECT name, fpath, cmd FROM tool_uses WHERE sid=? ORDER BY rowid", (s.sid,)).fetchall()
        self.assertIn(("exec_command", None, "pytest -q tests"), tu)
        self.assertEqual(sorted(f for n, f, c in tu if n == "apply_patch"), ["src/app.py", "tests/test_app.py"])
        v = check.verify(con)
        self.assertEqual(v["mismatch"], [])
        self.assertEqual(v["missing_files"], 0)
        day = views.day(con, "2026-09-30")
        self.assertIn("Codex", day)
        self.assertIn("코덱스로 테스트 고쳐줘", day)
        self.assertIn("테스트를 고쳤습니다", [r["snippet"] for r in views.search(con, "테스트를 고쳤")["rows"]])
        self.assertIn("· Codex", views.resume(con, PROJ))
        con.close()
        b = ingest.run()
        self.assertEqual(b["counts"].get("rows_new", 0), 0)

    def test_incremental_keeps_state(self):
        s = CodexSess()
        self.write(s, s.basic())
        ingest.run()
        # 이어 읽기: session_meta·turn_context 는 앞에 있었다. cwd·판은 저장된 행에서 되살려야 한다.
        self.write(s, s.human("두 번째 요청") + s.reply("두 번째 답") + s.done())
        r = ingest.run()
        self.assertEqual(r["accounting"]["mismatch_files"], 0)
        con = dbm.open_db()
        rows = con.execute("SELECT human, project FROM cards WHERE sid=? ORDER BY rowid", (s.sid,)).fetchall()
        self.assertEqual([h for h, p in rows], ["코덱스로 테스트 고쳐줘", "두 번째 요청"])
        raw = con.execute("SELECT raw FROM events WHERE sid=? ORDER BY off DESC LIMIT 1", (s.sid,)).fetchone()[0]
        last = json.loads(raw)
        self.assertEqual(last.get("cwd"), s.cwd)
        self.assertEqual(last.get("version"), s.ver)
        self.assertEqual(check.verify(con)["mismatch"], [])
        con.close()

    def test_secret_redacted_and_private_files_not_read(self):
        s = CodexSess()
        lines = s.basic() + s.human("export TOKEN=%s 로 다시 해봐" % FAKE_SECRET) + s.done()
        self.write(s, lines)
        # 읽으면 안 되는 파일: 들어 있는 값이 DB 에 들어가면 안 된다
        os.makedirs(self.env.codex, exist_ok=True)
        decoy = "DecoyAuthValue7731xyz"
        for f in ("auth.json", "config.toml"):
            with open(os.path.join(self.env.codex, f), "w") as fh:
                fh.write(json.dumps({"OPENAI_API_KEY": decoy}))
        rels = [r[0] for r in ingest.discover(self.env.claude)]
        self.assertFalse([r for r in rels if r.startswith("codex/") and "rollout-" not in r
                          and r != "codex/history.jsonl"], rels)
        a = ingest.run()
        self.assertEqual(a["accounting"]["mismatch_files"], 0)
        con = dbm.open_db()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        from pwikilib import paths
        with open(paths.db_path(), "rb") as fh:
            b = fh.read()
        self.assertNotIn(FAKE_SECRET.encode(), b)
        self.assertNotIn(decoy.encode(), b)
        res = check.redact_check(con)
        self.assertEqual(res["total_hits"], 0, json.dumps(res["locations"], ensure_ascii=False)[:800])
        con.close()

    def test_history_and_mixed_project(self):
        s = CodexSess()
        self.write(s, s.basic())
        c = Sess()
        self.env.write_lines(self.env.session_path(c.sid), [c.human("클로드 쪽 요청"), c.assistant_text("클로드 답")])
        os.makedirs(self.env.codex, exist_ok=True)
        self.env.write_lines(os.path.join(self.env.codex, "history.jsonl"),
                             [{"session_id": s.sid, "ts": 1790740800, "text": "코덱스로 테스트 고쳐줘"}])
        a = ingest.run()
        self.assertEqual(a["accounting"]["mismatch_files"], 0)
        con = dbm.open_db()
        projs = dict(con.execute("SELECT sid, project FROM sessions"))
        self.assertEqual(projs[s.sid], projs[c.sid])
        self.assertEqual(con.execute("SELECT count(*) FROM events WHERE kind='history'").fetchone()[0], 1)
        v = check.verify(con)
        self.assertEqual(v["mismatch"], [])
        self.assertEqual(v["history"]["missed"], 0)
        self.assertEqual(v["history"]["totals"].get("no_session_record", 0), 0)
        con.close()

    def test_injected_after_image_and_agents_md_and_v149_echo(self):
        s = CodexSess()
        img = s.line("response_item", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<image name=[Image #1]>"},
            {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="},
            {"type": "input_text", "text": "</image>"},
            {"type": "input_text", "text": "<environment_context>x</environment_context>"}]})
        agents = s.line("response_item", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "# AGENTS.md instructions for /x\n규칙"}]})
        echo149 = s.line("event_msg", {"type": "item_completed", "item": {"type": "UserMessage", "content": "진짜 요청"}})
        lines = [s.meta(), s.turn(), img, agents, s.human("진짜 요청")[0], echo149] + s.reply("답") + s.done()
        self.write(s, lines)
        a = ingest.run()
        self.assertEqual(a["accounting"]["mismatch_files"], 0)
        self.assertEqual(a["excluded"].get("codex:injected:environment_context"), 1)
        self.assertEqual(a["excluded"].get("codex:injected:agents_md"), 1)
        self.assertEqual(a["excluded"].get("codex:echo:user_message"), 1)
        con = dbm.open_db()
        self.assertEqual([r[0] for r in con.execute("SELECT human FROM cards WHERE sid=?", (s.sid,))], ["진짜 요청"])
        con.close()

    def test_missing_tool_ids_do_not_collide(self):
        s = CodexSess()
        fc = s.line("event_msg", {"type": "item_completed", "item": {"type": "FileChange",
                                                                     "changes": {"a.py": {}, "b.py": {}}}})
        lines = [s.meta(), s.turn()] + s.human("고쳐줘") + [fc] + s.done()
        self.write(s, lines)
        t = CodexSess()
        self.write(t, [t.meta(), t.turn()] + t.human("다른 세션") + [dict(fc, timestamp=t.ts())] + t.done())
        ingest.run()
        con = dbm.open_db()
        rows = con.execute("SELECT tuid, fpath FROM tool_uses WHERE name='file_change'").fetchall()
        self.assertEqual(len(rows), 4, rows)
        self.assertFalse([r for r in rows if r[0].startswith("None")], rows)
        con.close()

    def test_symlinked_codex_dirs_not_followed(self):
        outside = os.path.join(self.env.tmp, "outside")
        s = CodexSess()
        os.makedirs(self.env.codex)
        real_path = s.path(self.env).replace(self.env.codex, outside)
        self.env.write_lines(real_path, s.basic())
        os.symlink(os.path.join(outside, "sessions"), os.path.join(self.env.codex, "sessions"))
        self.assertFalse([r for r in ingest.discover(self.env.claude) if r[0].startswith("codex/")])
        os.unlink(os.path.join(self.env.codex, "sessions"))
        os.rmdir(self.env.codex)
        os.symlink(outside, self.env.codex)
        self.assertFalse([r for r in ingest.discover(self.env.claude) if r[0].startswith("codex/")])

    def test_exec_session_restored_as_sdk(self):
        s = CodexSess(source="exec")
        self.write(s, s.basic())
        ingest.run()
        self.write(s, s.human("자동 실행 두 번째") + s.done())
        ingest.run()
        con = dbm.open_db()
        kinds = [r[0] for r in con.execute("SELECT kind FROM events WHERE sid=? AND kind IN ('human','sdk') ORDER BY off",
                                           (s.sid,))]
        self.assertEqual(kinds, ["sdk", "sdk"])
        con.close()

    def test_schema1_db_gets_src_columns(self):
        con = dbm.open_db()
        dbm.init(con)
        for t in ("files", "sessions"):
            con.execute("ALTER TABLE %s DROP COLUMN src" % t)
        con.commit()
        self.assertFalse(dbm.has_column(con, "files", "src"))
        dbm.init(con)
        dbm.init(con)  # 멱등
        self.assertTrue(dbm.has_column(con, "files", "src"))
        self.assertTrue(dbm.has_column(con, "sessions", "src"))
        con.close()

    def test_no_codex_dir_lists_nothing(self):
        self.assertFalse(os.path.exists(self.env.codex))
        self.assertFalse([r for r in ingest.discover(self.env.claude) if r[0].startswith("codex/")])


if __name__ == "__main__":
    unittest.main()
