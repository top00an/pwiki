import json
import os
import sqlite3
import unittest

from helpers import FAKE_SECRET, Env, Sess
from pwikilib import check, ingest, paths
from pwikilib import export as exportm
from pwikilib import db as dbm
from pwikilib.redact import Checker, Redactor

CORE = FAKE_SECRET.rstrip("#$")


class TestRedactUnit(unittest.TestCase):
    def setUp(self):
        self.R = Redactor([FAKE_SECRET])
        self.C = Checker([FAKE_SECRET])

    def left(self, s):
        d = {}
        self.C.count_text(s, d)
        return d

    def test_known_value_variants(self):
        forms = [
            "plain %s end" % FAKE_SECRET,
            "shell -p'%s'" % FAKE_SECRET.replace("$", "\\$"),
            "double escaped %s" % FAKE_SECRET.replace("#", "\\\\#").replace("$", "\\\\$"),
            "url mysql://u:%s@h/db" % FAKE_SECRET.replace("#", "%23").replace("$", "%24"),
            "split %s\n%s tail" % (FAKE_SECRET[:5], FAKE_SECRET[5:]),
            "html %s" % FAKE_SECRET.replace("#", "&#35;").replace("$", "&#x24;"),
            "doubled $ %s" % FAKE_SECRET.replace("$", "$$"),
        ]
        for f in forms:
            r = self.R.text(f)
            self.assertNotIn(CORE, r, f)
            self.assertEqual(self.left(r), {}, f)

    def test_regex_forms(self):
        cases = {
            "dsn": "postgresql://app:s3cr3tPw@db:5432/x",
            "mysql_p": "mysql -u root -punknownPw1 -h 10.0.0.1",
            "pw_kv": "PASSWORD=hunter2hunter",
            "identified_by": "CREATE USER 'a'@'%' IDENTIFIED BY 'p4ss!word';",
            "bearer": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
            "anthropic_key": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
            "github_token": "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "aws_key": "AKIAABCDEFGHIJKLMNOP",
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nAAAA\n-----END RSA PRIVATE KEY-----",
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
            "phone": "담당 010-1234-5678 연락",
            "email": "메일 someone.name@example.co.kr 로",
        }
        for kind, s in cases.items():
            r = self.R.text(s)
            self.assertIn("[REDACTED:%s]" % kind, r, kind)
            self.assertEqual(self.left(r), {}, kind)
            self.assertEqual(self.R.text(r), r, "idempotent " + kind)

    def test_keep_work_knowledge(self):
        keep = ["서버 10.0.0.2 과 10.0.0.1", "ssh -p 22 root@ssh5.example.net", "uuid 01012345-6789-4abc-8def-0123456789ab",
                "cost 0.01012345678", "PGPASSWORD=$PGPASS psql", "pwd: /Users/x", "password=True"]
        for s in keep:
            self.assertEqual(self.R.text(s), s, s)

    def test_obj_walk_json_escape(self):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "mysql -uX -p'%s' -e 'select 1'" % FAKE_SECRET}}]}})
        self.assertIn("select 1", line)
        o = json.loads(line)
        r = self.R.obj(o)
        self.assertNotIn(CORE, json.dumps(r))


class TestRedactEndToEnd(unittest.TestCase):
    """가짜 비밀을 JSON 이스케이프·URL 인코딩·줄 분할·Write 본문·명령 인자에 넣고 적재·내보내기 뒤 0건을 확인한다."""

    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def test_canaries(self):
        s = Sess()
        lines = [s.human("접속해줘 비번은 %s 이야" % FAKE_SECRET)]
        o, t1 = s.tool_use("Bash", {"command": "mysql -u APPUSER -p'%s' -h 10.0.0.2 -e 'select 1'" %
                                               FAKE_SECRET.replace("$", "\\$")})
        lines.append(o)
        lines.append(s.tool_result(t1, "ERROR 1045 using password: YES (%s)" % FAKE_SECRET, is_error=True))
        o, t2 = s.tool_use("Write", {"file_path": "/tmp/conf.env",
                                     "content": "DSN=mysql://u:%s@h/db\nSPLIT=%s\n%s\nPHONE=010-9876-5432\n"
                                                % (FAKE_SECRET.replace("#", "%23").replace("$", "%24"),
                                                   FAKE_SECRET[:6], FAKE_SECRET[6:])})
        lines.append(o)
        lines.append(s.tool_result(t2, "File created"))
        lines.append(s.assistant_text("완료. 메일은 dev.person@example.com 로 보냈고 키 sk-ant-api03-zzzzzzzzzzzzzzzzzzzz"))
        path = self.env.session_path(s.sid)
        self.env.write_lines(path, lines)
        with open(path, "rb") as fh:
            raw = fh.read()
        self.assertIn(CORE.encode(), raw)
        summ = ingest.run()
        self.assertEqual(summ["accounting"]["mismatch_files"], 0)
        self.assertGreaterEqual(summ["redactions"].get("known", 0), 4)
        con = dbm.open_db()
        r = exportm.export(con)
        self.assertEqual(r["blocked"], 0)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for p in (paths.db_path(), paths.db_path() + "-wal"):
            if os.path.exists(p):
                with open(p, "rb") as fh:
                    b = fh.read()
                self.assertNotIn(CORE.encode(), b, p)
                self.assertNotIn(b"dev.person@example.com", b, p)
                self.assertNotIn(b"010-9876-5432", b, p)
        for dp, dn, fn in os.walk(self.env.vault):
            for f in fn:
                with open(os.path.join(dp, f), "rb") as fh:
                    self.assertNotIn(CORE.encode(), fh.read())
        res = check.redact_check(con)
        self.assertEqual(res["total_hits"], 0, json.dumps(res["locations"], ensure_ascii=False))
        n = con.execute("SELECT count(*) FROM fts WHERE fts MATCH ?", ('"%s"' % CORE,)).fetchone()[0]
        self.assertEqual(n, 0)
        n2 = con.execute("SELECT count(*) FROM fts WHERE body LIKE '%REDACTED:known%'").fetchone()[0]
        self.assertGreater(n2, 0)
        con.close()


class TestLiteralValue(unittest.TestCase):
    def test_curl_u_uid_gid_with_trailing_punctuation(self):
        from pwikilib import leakscan
        for v in ("1000", "1000.", "1000,", "1000)"):
            self.assertFalse(leakscan.literal_value(v, "curl_u"), v)
        # 숫자 뒤에 글자가 이어지면 여전히 값으로 센다
        self.assertTrue(leakscan.literal_value("1000.x9Zk", "curl_u"))
        self.assertTrue(leakscan.literal_value("s3cretPw.", "curl_u"))

    def test_middle_dot_list_is_not_a_value(self):
        from pwikilib import leakscan
        found = {}
        leakscan._scan_rules("# names: *password·*pwd(PGPASSWORD\u00b7dbPassword\u00b7MYSQL_PWD)", found)
        self.assertFalse([v for v in found if v.startswith("\u00b7")], found)
        self.assertFalse(leakscan.literal_value("\u00b7dbPassword", "kv"))
        self.assertFalse(leakscan.keep_value("\u00b7dbPassword9"))
        # 진짜 값은 그대로 잡는다
        found = {}
        leakscan._scan_rules("PGPASSWORD=Zq7xR2mK9vLp", found)
        self.assertIn("Zq7xR2mK9vLp", found)


if __name__ == "__main__":
    unittest.main()
