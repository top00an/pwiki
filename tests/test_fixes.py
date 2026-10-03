"""독립 검증 지적(2026-09-30) 재현 시험. 가짜 값만 쓴다."""
import hashlib
import json
import os
import unittest

from helpers import FAKE_SECRET, PROJ, Env, Sess, uid
from pwikilib import check, ingest, paths, views
from pwikilib import db as dbm
from pwikilib import export as exportm
from pwikilib.redact import Checker, Harvested, Redactor

CORE = FAKE_SECRET.rstrip("#$")


def _usage(out):
    return {"input_tokens": 2, "output_tokens": out, "cache_creation_input_tokens": 10, "cache_read_input_tokens": 100}


class TestRedactRules(unittest.TestCase):
    def setUp(self):
        self.R = Redactor([FAKE_SECRET])
        self.C = Checker([FAKE_SECRET])

    def left(self, s):
        d = {}
        self.C.count_text(s, d)
        return d

    def test_password_family_forms(self):
        """PGPASSWORD·ROOTPW·*_PW·*_PASS·dbPassword·sshpass·curl -u·aws_secret_access_key 의 값이 가려진다."""
        cases = ["PGPASSWORD=fakepw123 psql", "export ROOTPW='Fake99Root#'", "OWNER_PW=fakeowner123!",
                 "MDB_PW=fake_mdbpass9!", "DEMOAPP_BOOTSTRAP_PW=Boot1234x", "PW=Fake#a9b9cccc", "DB_PASS=Fak3pass9",
                 "dbPassword=Fak3pass!", "rootPw: Fak3root!", "sshpass -p 'Fake-9999' ssh root@h",
                 "curl -u admin:Fake-Pass-9999 http://x", "aws_secret_access_key=abcdEFGH1234ijklMNOP",
                 "MYSQL_PWD=Fake5555x"]
        for c in cases:
            r = self.R.text(c)
            self.assertIn("[REDACTED:", r, c)
            for frag in ("fakepw123", "Fake99Root", "fakeowner123", "fake_mdbpass9", "Boot1234x", "a9b9cccc", "Fak3pass",
                         "Fak3root", "Fake-9999", "Fake-Pass-9999", "abcdEFGH1234", "Fake5555x"):
                self.assertNotIn(frag, r, c)
            self.assertEqual(self.left(r), {}, c)

    def test_other_token_forms(self):
        for s, kind in (("xoxb-1234567890-abcdefghij", "slack_token"),
                        ("AIzaSyA1234567890abcdefghijklmnopqrstuv", "google_key"),
                        ("typesafeApiKey apikey_0123456789abcdef_fedcba9876543210", "apikey_hex"),
                        ("Tel010-1234-5678", "phone"), ("admin@example-corp.com", "email")):
            self.assertIn("[REDACTED:%s]" % kind, self.R.text(s), s)
        # 서버 주소(ssh 대상)는 남긴다
        self.assertEqual(self.R.text("ssh root@ssh5.example.net"), "ssh root@ssh5.example.net")

    def test_serialized_json_boundaries(self):
        """다시 직렬화된 JSON(\\n 이 리터럴) 줄 머리에서도 잡는다."""
        for s in ('line1\\nmysql -uroot -pFakeP4ss\\nnext', 'x\\nsk-ant-api03-abcdefghijklmnop',
                  'x\\nghp_abcdefghijklmnopqrstuvwxyz0123456789', '\\nAKIAABCDEFGHIJKLMNOP',
                  '{\\"password\\": \\"fakeJson99\\"}',
                  'a\\neyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U'):
            r = self.R.text(s)
            self.assertIn("[REDACTED:", r, s)
            self.assertEqual(self.left(r), {}, s)
        self.assertTrue(self.R.text('line1\\nmysql -uroot -pFakeP4ss\\nnext').endswith("\\nnext"))

    def test_partial_known_value(self):
        """알려진 값의 긴 조각(끝 글자가 빠진 값, 영숫자 핵심만 쓴 값)도 가린다. 글자만으로 된 조각은 남긴다."""
        for s in (FAKE_SECRET[:-1] + "\n", "'%s'" % CORE, "|%s|" % CORE, FAKE_SECRET[1:]):
            r = self.R.text(s)
            self.assertNotIn(CORE, r, repr(s))
            self.assertIn("[REDACTED:known", r)
            self.assertEqual(self.left(r), {}, repr(s))
        self.assertEqual(self.R.text("Zq7fakeS only"), "Zq7fakeS only")
        self.assertEqual(self.R.text("fakeSecret 이름"), "fakeSecret 이름")

    def test_hash_prefix_of_known_value(self):
        h = hashlib.sha256(FAKE_SECRET.encode()).hexdigest()
        for n in (6, 8, 64):
            r = self.R.text("h=%s 끝" % h[:n])
            self.assertIn("[REDACTED:known_hash]", r)
            self.assertNotIn(h[:n], r)
        # 다른 16진 토큰은 남긴다
        self.assertEqual(self.R.text("commit 0badc0ffee12"), "commit 0badc0ffee12")

    def test_truncated_token_is_not_a_secret(self):
        """출력이 가림 토큰을 '[REDAC…' 로 잘라도 검사기가 비밀로 세지 않는다."""
        self.assertEqual(self.left("mysql -uroot -p[REDAC…"), {})
        self.assertEqual(self.left("mysql -uroot -p'[REDAC…"), {})


class EnvCase(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()


class TestHarvest(EnvCase):
    def test_value_learned_in_password_context_is_redacted_elsewhere(self):
        """다른 문맥(owner: ·로그인 폼 입력)에 먼저 나온 값도, 같은 실행 안에서 비밀번호 문맥으로 잡히면 가린다."""
        pw = "Owner-7755x!"
        s = Sess()
        lines = [s.human("계정 정보: owner: %s 로 로그인" % pw)]
        o, t = s.tool_use("mcp__browser__fill", {"selector": "#pw", "value": pw})
        lines += [o, s.tool_result(t, "filled")]
        lines += [s.human("DB 접속"), ]
        o, t = s.tool_use("Bash", {"command": "export OWNER_PW='%s' && psql" % pw})
        lines += [o, s.tool_result(t, "ok")]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        r = ingest.run()
        self.assertGreaterEqual(r["harvest"]["strong_new"], 1)
        con = dbm.open_db()
        exportm.export(con)
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with open(paths.db_path(), "rb") as fh:
            self.assertNotIn(pw.encode(), fh.read())
        for dp, dn, fn in os.walk(self.env.vault):
            for f in fn:
                with open(os.path.join(dp, f), "rb") as fh:
                    self.assertNotIn(pw.encode(), fh.read())
        hp = os.path.join(self.env.home, "secrets.harvested")
        self.assertEqual(os.stat(hp).st_mode & 0o777, 0o600)
        res = check.redact_check(con)
        self.assertEqual(res["total_hits"], 0, json.dumps(res["locations"], ensure_ascii=False, default=str))
        con.close()
        # 다음 실행에는 저장된 수확값으로 새 줄도 가린다(원문에서 비밀번호 문맥이 사라진 뒤에도)
        s2 = Sess()
        self.env.write_lines(self.env.session_path(s2.sid), [s2.human("그 값 %s 다시" % pw)])
        ingest.run()
        con = dbm.open_db()
        self.assertEqual(con.execute("SELECT count(*) FROM events WHERE text LIKE ?", ("%" + pw + "%",)).fetchone()[0], 0)
        con.close()


class TestApikeyParts(EnvCase):
    def test_apikey_hex_parts_redacted_elsewhere(self):
        """apikey_<16진>_<16진> 의 16진 부분은 앞부분을 자리표시로 바꾼 글이나 부분만 인용한 글에서도 가린다."""
        a, b = "0123456789abcdef0123", "fedcba9876543210fedcba98765"
        s = Sess()
        self.env.write_lines(self.env.session_path(s.sid), [
            s.human("typesafeApiKey apikey_%s_%sQz 쓰는 중" % (a, b)),
            s.assistant_text('설정 예: "key": "<APIKEY>_%s" 그리고 앞부분 "%s"' % (b, a))])
        ingest.run()
        con = dbm.open_db()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with open(paths.db_path(), "rb") as fh:
            data = fh.read()
        for part in (a, b, a[:14], b[-18:]):
            self.assertNotIn(part.encode(), data, part)
        con.close()


class TestWorkflowRunDoc(EnvCase):
    def test_wf_run_result_redacted_as_object(self):
        """워크플로 실행 JSON 은 푼 객체째 가린다(다시 직렬화한 글에 가림을 걸면 줄 머리 규칙이 빗나간다)."""
        s = Sess()
        self.env.write_lines(self.env.session_path(s.sid), [s.human("워크플로")])
        wdir = os.path.join(self.env.claude, "projects", PROJ, s.sid, "workflows")
        os.makedirs(wdir)
        res = {"summary": "끝", "notes": "a\nmysql -uroot -pFakeP4ss\nsk-ant-api03-abcdefghijklmnop\n"
                                          "ghp_abcdefghijklmnopqrstuvwxyz0123456789\nAKIAABCDEFGHIJKLMNOP\n",
               "cfg": {"password": "fakeJson99"}}
        with open(os.path.join(wdir, "wf_cnry0001-001.json"), "w") as fh:
            json.dump({"runId": "wf_cnry0001-001", "workflowName": "t", "result": res, "phases": [{"note": "\nAKIAABCDEFGHIJKLMNOP"}]}, fh)
        ingest.run()
        con = dbm.open_db()
        text = con.execute("SELECT text FROM docs WHERE kind='wf_run'").fetchone()[0]
        for frag in ("FakeP4ss", "sk-ant-api03", "ghp_abcdef", "AKIAABCD", "fakeJson99"):
            self.assertNotIn(frag, text, frag)
        res = check.redact_check(con)
        self.assertEqual(res["total_hits"], 0, json.dumps(res["locations"], ensure_ascii=False, default=str))
        con.close()


class TestCheckerIndependence(EnvCase):
    def test_planted_leak_is_caught_by_independent_rules(self):
        """가림을 거치지 않고 DB 에 직접 넣은 리터럴을 독립 규칙이 잡는다(구현 규칙과 다른 규칙이 나란히 판정)."""
        s = Sess()
        self.env.write_lines(self.env.session_path(s.sid), [s.human("ROOTPW=Fake99Root#x 로 접속")])
        ingest.run()
        con = dbm.open_db()
        clean = check.redact_check(con)
        self.assertEqual(clean["total_hits"], 0)
        con.execute("INSERT INTO docs(path, kind, text) VALUES ('planted', 'test', ?)", ("owner PGPASSWORD=plantedpw1 끝",))
        dirty = check.redact_check(con)
        self.assertGreater(dirty["locations"]["ind_shape_db"]["hits"].get("kv", 0), 0)
        self.assertGreater(dirty["total_hits"], 0)
        con.close()

    def test_truncation_marker_is_not_a_literal(self):
        """내보내기가 자르며 붙인 '…(잘림)' 이 -p 뒤에 와도 독립 형태 규칙이 값으로 세지 않는다. 진짜 값은 센다."""
        from pwikilib.leakscan import ShapeCounter
        sc = ShapeCounter()
        sc.text("> mysql -uroot -p…(잘림)")
        self.assertEqual(dict(sc.c), {})
        sc.text("> mysql -uroot -pFake99x")
        self.assertEqual(dict(sc.c), {"mysql": 1})

    def test_fts_secure_delete_on(self):
        s = Sess()
        self.env.write_lines(self.env.session_path(s.sid), [s.human("x")])
        ingest.run()
        con = dbm.open_db()
        self.assertEqual(str(con.execute("SELECT v FROM fts_config WHERE k='secure-delete'").fetchone()[0]), "1")
        con.close()


class TestTokenAccounting(EnvCase):
    def _raw_out(self, con, kinds):
        best = {}
        for fid, in con.execute("SELECT fid FROM files WHERE kind IN (%s)" % ",".join("'%s'" % k for k in kinds)):
            for mid, uo in con.execute("SELECT msg_id, u_out FROM events WHERE fid=? AND msg_id IS NOT NULL", (fid,)):
                best[(fid, mid)] = max(best.get((fid, mid), 0), uo or 0)
        return sum(best.values())

    def test_resumed_workflow_counted_once(self):
        """재개한 Workflow 호출이 같은 runId 로 연결돼도 하위 토큰은 처음 띄운 카드에만 들어간다."""
        s = Sess()
        rid = "wf_0123abcd-456"
        lines = [s.human("워크플로 돌려")]
        o, t = s.tool_use("Workflow", {"script": "export const workflow = {name: '검증'}"})
        lines += [o, s.tool_result(t, "Workflow launched. runId: %s" % rid, tur={"runId": rid})]
        lines += [s.human("이어서 재개")]
        o, t = s.tool_use("Workflow", {"scriptPath": "x.js", "resume": True})
        lines += [o, s.tool_result(t, "Workflow resumed. runId: %s" % rid, tur={"runId": rid})]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        wd = os.path.join(self.env.claude, "projects", PROJ, s.sid, "subagents", "workflows", rid)
        a = Sess(sid=s.sid)
        self.env.write_lines(os.path.join(wd, "agent-a1.jsonl"), [a.assistant_text("x", mid="msg_w1", usage=_usage(300))])
        self.env.write_lines(os.path.join(wd, "agent-a2.jsonl"), [a.assistant_text("y", mid="msg_w2", usage=_usage(200))])
        ingest.run()
        con = dbm.open_db()
        subs = [r[0] for r in con.execute("SELECT sub_tok_out FROM cards ORDER BY seq")]
        self.assertEqual(subs, [500, 0])
        self.assertEqual(sum(subs), self._raw_out(con, ["wf_agent", "subagent"]))
        con.close()

    def test_teammate_session_without_human_input(self):
        """사람 입력이 없는 세션(팀원)도 '사람 입력 없음' 카드 한 장으로 토큰·에러가 합산된다."""
        s = Sess()
        lines = [s.base("user", message={"role": "user", "content": "<teammate-message teammate_id=\"team-lead\">\n검토해"
                                                                      "</teammate-message>"})]
        lines.append(s.assistant_text("검토 시작", mid="msg_t1", usage=_usage(70)))
        o, t = s.tool_use("Bash", {"command": "false"}, mid="msg_t2", usage=_usage(30))
        lines += [o, s.tool_result(t, "Exit code 1", is_error=True)]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        ingest.run()
        con = dbm.open_db()
        rows = con.execute("SELECT human_kind, delivered, tok_out, n_err, human FROM cards").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:4], ("no_human", 1, 100, 1))
        self.assertIn("teammate-message", rows[0][4])
        self.assertEqual(self._raw_out(con, ["session"]), 100)
        e = views.eff(con)
        self.assertIn("| 사람 입력 없음(팀원 세션 등) | 1 | 100 |", e)
        self.assertIn("| 원문 | - | 100 | - | 1 | 0 |", e)
        d = views.day(con, "2026-09-30")
        self.assertIn("사람 입력 0건", d)
        self.assertIn("사람 입력 없음(팀원 세션 등) 1장", d)
        con.close()

    def test_resume_ignores_teammate_session(self):
        """나중에 돈 팀원 세션('사람 입력 없음' 카드)이 있어도 resume 은 사람 입력이 가장 최근인 세션을 고른다."""
        h = Sess(t0="2026-09-30T01:00:00")
        self.env.write_lines(self.env.session_path(h.sid), [h.human("사람이 친 요청"), h.assistant_text("답")])
        t = Sess(t0="2026-09-30T03:00:00")
        self.env.write_lines(self.env.session_path(t.sid), [
            t.base("user", message={"role": "user", "content": "<teammate-message teammate_id=\"team-lead\">일해"
                                                                "</teammate-message>"}),
            t.assistant_text("팀원 답")])
        ingest.run()
        con = dbm.open_db(readonly=True)
        out = views.resume(con, PROJ)
        self.assertIn("- 세션: %s" % h.sid, out)
        self.assertIn("사람이 친 요청", out)
        self.assertIn("(사람 입력 없음)", out)
        con.close()

    def test_message_split_across_queued_boundary_counted_once(self):
        """한 응답(message.id)의 줄 사이에 작업 도중 입력이 끼어도 세션 안에서 한 번만 센다(최대 usage 로)."""
        s = Sess()
        lines = [s.human("요청"), s.assistant_text("앞", mid="msg_x", usage=_usage(10)),
                 s.attachment({"type": "queued_command", "prompt": "끼어든 입력", "commandMode": "prompt",
                               "origin": {"kind": "human"}}),
                 s.assistant_text("뒤", mid="msg_x", usage=_usage(90))]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        ingest.run()
        con = dbm.open_db()
        outs = [r[0] for r in con.execute("SELECT tok_out FROM cards ORDER BY seq")]
        self.assertEqual(outs, [90, 0])
        con.close()


class TestDayBundles(EnvCase):
    def test_queue_pairing_sdk_and_noarg_slash(self):
        s = Sess()
        lines = [s.human("첫 요청")]
        # 이미지 자리표시: queue-operation 에는 없고 전달 줄에만 [이미지] 가 붙는다
        lines += [s.plain("queue-operation", operation="enqueue", timestamp=s.ts(), content="[Image #3] 이거 봐")]
        lines += [s.attachment({"type": "queued_command", "prompt": [{"type": "text", "text": "[Image #3] 이거 봐"},
                                                                     {"type": "image", "source": {"data": "", "media_type": "image/png"}}],
                                "commandMode": "prompt", "origin": {"kind": "human"}})]
        # 슬래시·'!' 입력: 태그 모양으로 전달된다
        lines += [s.plain("queue-operation", operation="enqueue", timestamp=s.ts(), content="/reload-plugins"),
                  s.base("user", message={"role": "user", "content": "<command-name>/reload-plugins</command-name>\n"
                                                                     "<command-message>reload-plugins</command-message>\n"
                                                                     "<command-args></command-args>"})]
        lines += [s.plain("queue-operation", operation="enqueue", timestamp=s.ts(), content="pkill -f x.py"),
                  s.base("user", message={"role": "user", "content": "<bash-input>pkill -f x.py</bash-input>"})]
        # 예약 작업 입력: meta 줄로 전달된다(사람 입력 아님)
        lines += [s.plain("queue-operation", operation="enqueue", timestamp=s.ts(), content="[자동 점검] bash run.sh"),
                  s.base("user", message={"role": "user", "content": "[자동 점검] bash run.sh"}, isMeta=True)]
        # 같은 글의 사람 줄이 대기열 기록보다 먼저 적힌 경우
        lines += [s.human("먼저 적힌 입력"),
                  s.plain("queue-operation", operation="enqueue", timestamp=s.ts(), content="먼저 적힌 입력")]
        # 정말 전달되지 않은 입력
        lines += [s.plain("queue-operation", operation="enqueue", timestamp=s.ts(), content="취소한 입력"),
                  s.plain("queue-operation", operation="remove", timestamp=s.ts())]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        sd = Sess(t0="2026-09-30T02:00:00")
        # 실제 sdk 줄: origin 없음, promptSource=sdk, entrypoint=sdk-cli
        sdk_line = sd.base("user", message={"role": "user", "content": "자동 리뷰"}, promptSource="sdk", entrypoint="sdk-cli")
        self.env.write_lines(self.env.session_path(sd.sid, proj="-private-tmp-claude-batchjob-meta-51234"),
                             [sdk_line, sd.assistant_text("끝")])
        ingest.run()
        con = dbm.open_db()
        ghosts = [r[0] for r in con.execute("SELECT human FROM cards WHERE delivered=0")]
        self.assertEqual(ghosts, ["취소한 입력"])
        d = views.day(con, "2026-09-30")
        # 사람 입력 = 카드(첫 요청·이미지 입력·!입력·먼저 적힌 입력) 4 + 인자 없는 슬래시 1
        self.assertIn("사람 입력 5건 = 카드 4장 + 인자 없는 슬래시 명령 1건(카드 아님)", d)
        self.assertIn("자동 실행(claude -p) 1장", d)
        self.assertIn("전달 안 된 대기열 1건", d)
        self.assertIn("-private-tmp-claude-batchjob-meta-* 1", d)
        self.assertNotIn("## -private-tmp", d)
        e = views.eff(con)
        self.assertIn("사람 입력 카드 4장(따로: 자동 실행 1장", e)
        con.close()


if __name__ == "__main__":
    unittest.main()
