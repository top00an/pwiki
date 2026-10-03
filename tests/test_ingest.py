import fcntl
import json
import os
import unittest

from helpers import PROJ, Env, Sess, uid
from pwikilib import check, ingest, paths
from pwikilib import db as dbm


def basic_session(s):
    out = [s.plain("permission-mode", permissionMode="auto"),
           s.attachment({"type": "hook_success", "hookName": "SessionStart", "content": "", "stdout": "{}"}),
           s.human("첫 요청: 파일 만들어줘")]
    o, t = s.tool_use("Write", {"file_path": "/tmp/a.py", "content": "print(1)"})
    out += [o, s.tool_result(t, "File created successfully"), s.assistant_text("만들었다"),
            s.system("turn_duration", durationMs=4000, messageCount=5),
            s.plain("last-prompt", leafUuid=uid(), lastPrompt="첫 요청"),
            s.plain("ai-title", aiTitle="파일 만들기")]
    return out


class TestIngest(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def rows(self):
        con = dbm.open_db()
        n = con.execute("SELECT count(*) FROM events").fetchone()[0]
        con.close()
        return n

    def test_idempotent_and_accounting(self):
        s = Sess()
        p = self.env.session_path(s.sid)
        self.env.write_lines(p, basic_session(s))
        a = ingest.run()
        self.assertGreater(a["counts"]["rows_new"], 0)
        self.assertEqual(a["accounting"]["mismatch_files"], 0)
        t = a["accounting"]["totals"]
        self.assertEqual(t["uuid_lines"], t["stored_uuid"] + t["excluded_uuid"])
        self.assertEqual(t["lines"], t["stored_all"] + t["excluded_all"])
        self.assertEqual(a["excluded"].get("hook_success"), 1)
        self.assertEqual(a["excluded"].get("state:last-prompt"), 1)
        for fp in (paths.db_path(), paths.db_path() + "-wal", paths.db_path() + "-shm"):
            if os.path.exists(fp):
                self.assertEqual(os.stat(fp).st_mode & 0o777, 0o600, fp)
        n1 = self.rows()
        b = ingest.run()
        self.assertEqual(b["counts"].get("rows_new", 0), 0)
        self.assertEqual(b["counts"].get("lines_read", 0), 0)
        self.assertEqual(b["counts"].get("cards_built", 0), 0)
        self.assertEqual(self.rows(), n1)
        con = dbm.open_db()
        v = check.verify(con)
        self.assertEqual(v["mismatch"], [])
        self.assertEqual(v["totals"]["raw_uuid"], v["totals"]["stored_uuid"] + v["totals"]["excluded_uuid"])
        con.close()

    def test_append_and_partial_line(self):
        s = Sess()
        p = self.env.session_path(s.sid)
        self.env.write_lines(p, basic_session(s))
        ingest.run()
        n1 = self.rows()
        extra = s.human("두 번째 요청")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(extra, ensure_ascii=False)[:30])
        r = ingest.run()
        self.assertEqual(r["counts"].get("rows_new", 0), 0, "끝 개행 없는 줄은 확정하지 않는다")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(extra, ensure_ascii=False)[30:] + "\n")
        r = ingest.run()
        self.assertEqual(r["counts"]["rows_new"], 1)
        self.assertEqual(self.rows(), n1 + 1)
        con = dbm.open_db()
        self.assertEqual(con.execute("SELECT count(*) FROM cards").fetchone()[0], 2)
        con.close()

    def test_rewrite_resets_and_dedups(self):
        s = Sess()
        p = self.env.session_path(s.sid)
        lines = basic_session(s)
        self.env.write_lines(p, lines)
        ingest.run()
        n1 = self.rows()
        # 같은 내용으로 새 inode 파일을 만들고 앞에 한 줄을 더 끼워 오프셋을 밀어 본다.
        os.remove(p)
        self.env.write_lines(p, [s.plain("mode", mode="normal")] + lines, mode="w")
        r = ingest.run()
        self.assertEqual(r["counts"].get("files_reset"), 1)
        self.assertEqual(r["counts"]["rows_new"], 1, "새로 낀 줄만 추가된다")
        self.assertEqual(self.rows(), n1 + 1)
        self.assertEqual(r["accounting"]["mismatch_files"], 0)
        # 파일이 줄어든 경우
        self.env.write_lines(p, lines[:3], mode="w")
        r = ingest.run()
        self.assertEqual(r["counts"].get("files_reset"), 1)
        self.assertEqual(r["counts"].get("rows_new", 0), 0)
        self.assertEqual(self.rows(), n1 + 1, "원천에서 사라진 줄도 DB 에 보존된다")

    def test_subagent_workflow_memory_history(self):
        s = Sess()
        p = self.env.session_path(s.sid)
        lines = [s.human("서브에이전트로 검토해"), s.human("붙여넣은 긴 글의 첫 줄입니다 그리고 더\n둘째 줄\n셋째 줄")]
        o, t = s.tool_use("Agent", {"description": "코드 검토", "prompt": "검토해", "subagent_type": "general-purpose"})
        lines += [o, s.tool_result(t, "Async agent launched successfully.\nagentId: abc123def4567890",
                                   tur={"agentId": "abc123def4567890", "status": "async_launched"})]
        self.env.write_lines(p, lines)
        sd = os.path.join(self.env.claude, "projects", PROJ, s.sid, "subagents")
        sub = Sess(sid=s.sid)
        a1 = sub.base("user", message={"role": "user", "content": "[목표] 검토"}, isSidechain=True, agentId="abc123def4567890")
        a2 = sub.assistant_text("검토 끝", mid="msg_sub1", usage={"input_tokens": 1, "output_tokens": 77,
                                                                "cache_creation_input_tokens": 0,
                                                                "cache_read_input_tokens": 0})
        a2["isSidechain"] = True
        self.env.write_lines(os.path.join(sd, "agent-abc123def4567890.jsonl"), [a1, a2])
        with open(os.path.join(sd, "agent-abc123def4567890.meta.json"), "w") as fh:
            json.dump({"agentType": "general-purpose", "description": "코드 검토", "toolUseId": t}, fh)
        wd = os.path.join(sd, "workflows", "wf_0123abcd-456")
        self.env.write_lines(os.path.join(wd, "journal.jsonl"), [{"type": "launched"}, {"type": "result", "key": "k",
                                                                                       "result": {"verdict": "통과"}}])
        md = os.path.join(self.env.claude, "projects", PROJ, "memory")
        os.makedirs(md)
        with open(os.path.join(md, "note.md"), "w") as fh:
            fh.write("# 메모\n함정 하나\n")
        hist = os.path.join(self.env.claude, "history.jsonl")
        self.env.write_lines(hist, [{"display": "서브에이전트로 검토해", "pastedContents": {}, "timestamp": 1790000000000,
                                     "project": "/Users/tester/work/demo", "sessionId": s.sid},
                                    {"display": "기록에만 남은 프롬프트", "pastedContents": {}, "timestamp": 1790000001000,
                                     "project": "/Users/tester/work/demo", "sessionId": uid()},
                                    {"display": "[Pasted text #1 +3 lines]", "pastedContents": {"1": {
                                        "id": 1, "type": "text", "content": "붙여넣은 긴 글의 첫 줄입니다 그리고 더"}},
                                     "timestamp": 1790000002000, "project": "/Users/tester/work/demo", "sessionId": s.sid}])
        r = ingest.run()
        self.assertEqual(r["accounting"]["mismatch_files"], 0)
        self.assertEqual(r["kinds"].get("subagent"), 1)
        self.assertEqual(r["kinds"].get("journal"), 1)
        self.assertEqual(r["kinds"].get("memory"), 1)
        self.assertEqual(r["kinds"].get("history"), 1)
        con = dbm.open_db()
        card = con.execute("SELECT n_sub, sub_tok_out, features FROM cards ORDER BY seq DESC").fetchone()
        self.assertEqual(card[0], 1)
        self.assertEqual(card[1], 77)
        self.assertEqual(json.loads(card[2])["verify_agent"], 1)
        hp = con.execute("SELECT project FROM events WHERE kind='history'").fetchall()
        self.assertEqual({x[0] for x in hp}, {PROJ})
        # 메모리 md: 안 바뀌면 다시 읽지 않고, 바뀌면 전체를 다시 읽는다.
        con.close()
        r = ingest.run()
        self.assertEqual(r["counts"].get("docs_changed", 0), 0)
        with open(os.path.join(md, "note.md"), "w") as fh:
            fh.write("# 메모\n함정 하나\n고친 내용\n")
        r = ingest.run()
        self.assertEqual(r["counts"].get("docs_changed"), 1)
        con = dbm.open_db()
        self.assertIn("고친 내용", con.execute("SELECT text FROM docs WHERE kind='memory'").fetchone()[0])
        h = check.history_crosscheck(con)
        self.assertEqual(h["totals"].get("matched"), 2)
        self.assertEqual(h["totals"].get("no_session_record"), 1)
        con.close()

    def test_flock_prevents_concurrent_run(self):
        paths.ensure_home()
        fd = os.open(paths.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(ingest.Busy):
                ingest.run()
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_unknown_type_stored_redacted(self):
        """모르는 type·첨부는 가린 뒤 kind=unknown 으로 적재하고 개수도 남긴다(원문이 30일 뒤 지워져도 남는다)."""
        s = Sess()
        p = self.env.session_path(s.sid)
        fu = uid()
        self.env.write_lines(p, [s.human("x"),
                                 {"type": "future-type", "uuid": fu, "sessionId": s.sid, "timestamp": s.ts(),
                                  "version": "9.9.9", "note": "새 형식 표본 PGPASSWORD=fakepw123 끝"},
                                 s.attachment({"type": "task_status", "status": "done", "detail": "첨부 표본"}),
                                 "{bad json"])
        r = ingest.run()
        self.assertEqual(r["unknown_kept"].get("unknown_type:future-type"), 1)
        self.assertEqual(r["unknown_kept"].get("unknown_attachment:task_status"), 1)
        self.assertNotIn("unknown_type:future-type", r["excluded"])
        self.assertEqual(r["excluded"].get("bad_json"), 1)
        self.assertEqual(r["counts"].get("bad_json"), 1)
        self.assertEqual(r["accounting"]["mismatch_files"], 0)
        con = dbm.open_db()
        row = con.execute("SELECT kind, sub, text, raw FROM events WHERE key=?", ("%s:%s" % (s.sid, fu),)).fetchone()
        self.assertEqual(row[0], "unknown")
        self.assertEqual(row[1], "type:future-type")
        self.assertIn("새 형식 표본", row[2])
        self.assertNotIn("fakepw123", row[2] + row[3])
        self.assertTrue(views_search(con, "새 형식 표본"))
        v = check.verify(con)
        subs = {u[0]: u[1] for u in v["formats"]["unknown"]}
        self.assertEqual(subs, {"type:future-type": 1, "attachment:task_status": 1})
        self.assertIn("9.9.9", [x[0] for x in v["formats"]["by_version"]])
        con.close()


def views_search(con, q):
    from pwikilib import views
    return views.search(con, q)["rows"]


if __name__ == "__main__":
    unittest.main()
