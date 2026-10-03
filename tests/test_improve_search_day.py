"""개선 3(b)(c)(d)·4: 검색 기본 제외·bm25 우선·pwiki show·day 묶기. 가짜 비밀값만 쓴다."""
import contextlib
import io
import json
import os
import re
import unittest

from helpers import FAKE_SECRET, PROJ, Env, Sess

from pwikilib import cli, ingest, views
from pwikilib import db as dbm


class Base(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def run_cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()


class TestSearchScope(Base):
    def build(self):
        s = Sess()
        lines = [s.human("본선 검색표적어 확인"), s.assistant_text("본선 답")]
        o, t = s.tool_use("Agent", {"description": "검토", "prompt": "검토해", "subagent_type": "general-purpose"})
        lines += [o, s.tool_result(t, "Async agent launched successfully.\nagentId: abc123def4567890",
                                   tur={"agentId": "abc123def4567890", "status": "async_launched"})]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        sd = os.path.join(self.env.claude, "projects", PROJ, s.sid, "subagents")
        sub = Sess(sid=s.sid)
        a = sub.assistant_text("하위 에이전트의 검색표적어 답")
        a["isSidechain"] = True
        self.env.write_lines(os.path.join(sd, "agent-abc123def4567890.jsonl"), [a])
        with open(os.path.join(sd, "agent-abc123def4567890.meta.json"), "w") as fh:
            json.dump({"agentType": "general-purpose", "description": "검색표적어 검토", "toolUseId": t}, fh)
        wd = os.path.join(sd, "workflows", "wf_0123abcd-456")
        w = Sess(sid=s.sid)
        self.env.write_lines(os.path.join(wd, "agent-w1.jsonl"), [w.assistant_text("워크플로 팀원의 검색표적어 답")])
        self.env.write_lines(os.path.join(wd, "journal.jsonl"),
                             [{"type": "result", "key": "k", "result": {"verdict": "검색표적어 통과"}}])
        wdir = os.path.join(self.env.claude, "projects", PROJ, s.sid, "workflows")
        os.makedirs(wdir)
        with open(os.path.join(wdir, "wf_0123abcd-456.json"), "w") as fh:
            json.dump({"runId": "wf_0123abcd-456", "workflowName": "t", "summary": "검색표적어 요약"}, fh)
        ingest.run()
        return s

    def test_default_excludes_workflow_and_subagent_files(self):
        s = self.build()
        con = dbm.open_db(readonly=True)
        rows = views.search(con, "검색표적어", limit=50)["rows"]
        kinds = sorted(r["kind"] for r in rows)
        self.assertEqual(kinds, ["card", "human"], kinds)
        allr = views.search(con, "검색표적어", limit=50, include_all=True)["rows"]
        akinds = {r["kind"] for r in allr}
        self.assertTrue({"result", "doc:agent_meta", "doc:wf_run"} <= akinds, akinds)
        self.assertEqual(sum(1 for r in allr if r["kind"] == "assistant"), 2)
        con.close()
        rc, out = self.run_cli("search", "검색표적어")
        self.assertEqual(rc, 0)
        self.assertNotIn("[result]", out)
        self.assertIn("--all", out)
        rc, out = self.run_cli("search", "검색표적어", "--all")
        self.assertIn("[result]", out)
        self.assertIn("[doc:wf_run]", out)


class TestSearchOrder(Base):
    def test_bm25_before_matched_count(self):
        """점수는 bm25 × 일치 비율². 긴 행(채움말 700개)이 낱말을 다 담아도 짧고 촘촘한 2/3 행이 먼저다."""
        s = Sess()
        lines = []
        for i in range(12):
            lines += [s.human("잡담 %d" % i), s.assistant_text("관계없는 답 %d 번째 줄" % i)]
        filler = " ".join("채움말%03d" % i for i in range(700))
        lines += [s.human("긴 것"), s.assistant_text("zebrafish quokka narwhal " + filler)]
        lines += [s.human("짧은 것"), s.assistant_text("zebrafish quokka")]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        ingest.run()
        con = dbm.open_db(readonly=True)
        r = views.search(con, "zebrafish quokka narwhal", kind="assistant", per_session=0)
        self.assertEqual(len(r["rows"]), 2)
        self.assertEqual(r["rows"][0]["snippet"].strip(), "zebrafish quokka")
        self.assertEqual(r["rows"][0]["matched"], 2)
        con.close()

    def test_rows_without_time_go_last(self):
        s = Sess()
        self.env.write_lines(self.env.session_path(s.sid), [s.human("zq 확인"), s.assistant_text("응")])
        wd = os.path.join(self.env.claude, "projects", PROJ, s.sid, "subagents", "workflows", "wf_0123abcd-456")
        self.env.write_lines(os.path.join(wd, "journal.jsonl"), [{"type": "result", "key": "k", "result": {"v": "zq"}}])
        ingest.run()
        con = dbm.open_db(readonly=True)
        rows = views.search(con, "zq", include_all=True, per_session=0)["rows"]
        self.assertGreaterEqual(len(rows), 2)
        self.assertTrue(rows[0]["ts"])
        self.assertFalse(rows[-1]["ts"])
        con.close()


class TestShow(Base):
    def build(self):
        s = Sess()
        mid = "가운데표식" + "x" * 10
        big = "머리표식 " + ("앞" * 2700) + mid + ("뒤" * 900)
        lines = [s.human("비밀 %s 로 접속해서 로그 봐" % FAKE_SECRET)]
        o, t = s.tool_use("Bash", {"command": "cat big.log"})
        lines += [o, s.tool_result(t, big), s.assistant_text("로그를 끝까지 읽었다. 결론은 디스크 부족이다.")]
        self.env.write_lines(self.env.session_path(s.sid), lines)
        md = os.path.join(self.env.claude, "projects", PROJ, "memory")
        os.makedirs(md)
        with open(os.path.join(md, "note.md"), "w") as fh:
            fh.write("# 메모\n함정 하나 쇼표식\n")
        ingest.run()
        return s, mid

    def test_show_keys_as_printed(self):
        s, mid = self.build()
        con = dbm.open_db(readonly=True)
        res = views.search(con, "머리표식 로그 쇼표식", limit=20)
        out = views.format_search(res, "q")
        con.close()
        refs = re.findall(r"^- \[[^\]]+\] (.+?) · ", out, re.M)
        self.assertTrue(refs)
        seen_mid = False
        for ref in refs:
            rc, txt = self.run_cli("show", *ref.split(" "))
            self.assertEqual(rc, 0, ref)
            self.assertNotIn(FAKE_SECRET, txt)
            seen_mid = seen_mid or mid in txt
        self.assertTrue(seen_mid, "fts 본문은 가운데가 잘리지만 show 는 DB 의 전문을 낸다")
        # 카드: 사람 입력과 마지막 답 전문
        con = dbm.open_db(readonly=True)
        ck = con.execute("SELECT key FROM cards").fetchone()[0]
        con.close()
        rc, txt = self.run_cli("show", "카드", ck[2:])
        self.assertEqual(rc, 0)
        self.assertIn("로그를 끝까지 읽었다. 결론은 디스크 부족이다.", txt)
        self.assertIn("로 접속해서 로그 봐", txt)
        self.assertNotIn(FAKE_SECRET, txt)
        # day 의 짧은 키(sid8/uuid8)와 [ ] 감싼 키
        rc2, txt2 = self.run_cli("show", "[%s]" % views.short_key(ck))
        self.assertEqual(rc2, 0)
        self.assertEqual(txt2, txt)
        rc, txt = self.run_cli("show", "문서", "projects/%s/memory/note.md" % PROJ)
        self.assertEqual(rc, 0)
        self.assertIn("쇼표식", txt)
        rc, txt = self.run_cli("show", "없는키:없는값")
        self.assertEqual(rc, 1)


def _slash(s, name, args):
    return s.base("user", message={"role": "user", "content": "<command-name>%s</command-name>\n"
                                                              "<command-message>%s</command-message>\n"
                                                              "<command-args>%s</command-args>" % (name, name[1:], args)},
                  promptId="p-" + name)


class TestDay(Base):
    def test_grouped_goal_last_answer_and_fold(self):
        a = Sess(t0="2026-09-30T01:00:00")
        la = [a.human("문서 정리 시작해 줘")]
        o, t = a.tool_use("Workflow", {"script": "export const workflow = {name: 'docs-cleanup-batch'}"})
        la += [o, a.tool_result(t, "Workflow launched. runId: wf_0123abcd-456", tur={"runId": "wf_0123abcd-456"}),
               a.assistant_text("정리 워크플로를 띄웠다")]
        la += [_slash(a, "/goal", "모든 시험이 통과할 때까지 반복"), a.assistant_text("목표를 걸었다")]
        la += [a.human("ㄱㄱ"), a.assistant_text("계속 진행한다")]
        la += [a.human("남은시간"), a.assistant_text("빌드는 약 50분 남았습니다")]
        la += [a.human("", content=[{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}}]),
               a.assistant_text("그림을 봤다. 화면 깨짐은 글꼴 문제다")]
        la += [a.human("다음은 정리해 줘"), a.assistant_text("정리했다")]
        self.env.write_lines(self.env.session_path(a.sid), la)
        b = Sess(t0="2026-09-30T02:00:00")
        self.env.write_lines(self.env.session_path(b.sid), [b.human("다른 세션의 일 해 줘"), b.assistant_text("다른 일 끝")])
        ingest.run()
        con = dbm.open_db(readonly=True)
        d = views.day(con, "2026-09-30")
        con.close()
        self.assertIn("사람 입력 7건 = 카드 7장", d)
        # 세션 묶음 머리말(세션 id 앞 8자, 워크플로 이름)
        heads = [ln for ln in d.splitlines() if ln.startswith("### ")]
        self.assertEqual(len(heads), 2, d)
        self.assertIn(a.sid[:8], heads[0])
        self.assertIn("docs-cleanup-batch", heads[0])
        self.assertIn(b.sid[:8], heads[1])
        # /goal 인자
        self.assertIn("/goal 모든 시험이 통과할 때까지", d)
        self.assertNotIn("<command-name>", d)
        # 카드마다 마지막 답 60자
        self.assertIn("→ 정리 워크플로를 띄웠다", d)
        self.assertIn("→ 다른 일 끝", d)
        # 4자 이하·이미지 전용 줄 3장은 한 줄로 접고, 접은 줄에는 마지막 답을 싣는다
        fold = [ln for ln in d.splitlines() if "짧은 입력 3장" in ln]
        self.assertEqual(len(fold), 1, d)
        self.assertIn("ㄱㄱ", fold[0])
        self.assertIn("남은시간", fold[0])
        self.assertIn("→ 그림을 봤다. 화면 깨짐은 글꼴 문제다", d)
        self.assertNotIn("→ 계속 진행한다", d)
        self.assertEqual(sum(1 for ln in d.splitlines() if ln.startswith("- ")), 5, d)


if __name__ == "__main__":
    unittest.main()
