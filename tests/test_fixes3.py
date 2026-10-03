"""독립 검증 3차 지적(2026-10-01) 재현 시험. 가짜 값만 쓴다."""
import json
import os
import unittest

from helpers import CWD, FAKE_SECRET, Env, Sess, uid
from pwikilib import check, ingest, leakscan, paths, rederive, views
from pwikilib import db as dbm
from pwikilib.redact import Checker, Harvested, Redactor, value_strength

LATE = "Qx5lateFake31Zk"    # 늦게 수확되는 가짜 값
UESC = "Kv7uescFake20Pm"    # 인코딩 구분자 뒤 가짜 값


def _hist(env, rows):
    env.write_lines(os.path.join(env.claude, "history.jsonl"),
                    [{"display": d, "pastedContents": {}, "timestamp": 1790000000000 + i * 1000, "project": CWD,
                      "sessionId": sid} for i, (sid, d) in enumerate(rows)])


def _goal_midturn(s, cond):
    """작업 도중 '/goal <글>' 의 실측 모양: enqueue · goal_status · isMeta · remove · queued_command(auto-continuation)."""
    tu, tuid = s.tool_use("Bash", {"command": "ls"})
    return [s.plain("queue-operation", operation="enqueue", timestamp=s.ts(), content="Goal set: " + cond),
            s.attachment({"type": "goal_status", "met": False, "sentinel": True, "condition": cond}),
            s.base("user", message={"role": "user", "content": "A session-scoped Stop hook is now active with condition:"
                                                                 " \"%s\". Briefly acknowledge the goal" % cond},
                   isMeta=True, promptId=uid()),
            tu, s.tool_result(tuid, "a.txt"),
            s.plain("queue-operation", operation="remove", timestamp=s.ts(), content="Goal set: " + cond,
                    reason="absorbed_mid_turn"),
            s.attachment({"type": "queued_command", "prompt": "Goal set: " + cond, "commandMode": "prompt",
                          "origin": {"kind": "auto-continuation"}}),
            s.assistant_text("목표 확인했습니다")]


def _goal_idle(s, cond):
    """한가할 때 친 '/goal <글>': <command-name> 사람 줄과 local-command-stdout 이 남는다(첨부 queued_command 없음)."""
    return [s.base("user", message={"role": "user", "content": "<command-name>/goal</command-name>\n"
                                                               "<command-message>goal</command-message>\n"
                                                               "<command-args>%s</command-args>" % cond},
                   promptId=uid()),
            s.base("user", message={"role": "user", "content": "<local-command-stdout>Goal set: %s</local-command-stdout>"
                                                               % cond}, promptId=uid()),
            s.attachment({"type": "goal_status", "met": False, "sentinel": True, "condition": cond}),
            s.assistant_text("목표 접수")]


def _db_hits(con, needle):
    """모든 표의 문자열 칸(FTS 그림자 표 포함)에서 needle 이 든 칸 수."""
    n = 0
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        if t == "fts":
            continue
        cols = [r[1] for r in con.execute('PRAGMA table_info("%s")' % t)]
        for row in con.execute('SELECT %s FROM "%s"' % (", ".join('"%s"' % c for c in cols), t)):
            for v in row:
                if isinstance(v, str) and needle in v:
                    n += 1
                elif isinstance(v, (bytes, bytearray)) and needle.encode() in v:
                    n += 1
    return n


class EnvCase(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()


class TestGoalMidTurn(EnvCase):
    def test_goal_card_and_crosscheck(self):
        s = Sess()
        lines = [s.human("첫 요청 clear 하고 시작"), s.assistant_text("작업 중")] + _goal_midturn(s, "끝까지 진행하고 보고해") \
            + _goal_idle(s, "다른 목표로 마무리")
        self.env.write_lines(self.env.session_path(s.sid), lines)
        _hist(self.env, [(s.sid, "첫 요청 clear 하고 시작"), (s.sid, "/goal 끝까지 진행하고 보고해"),
                         (s.sid, "/goal 다른 목표로 마무리"), (s.sid, "/goal clear"), (s.sid, "/btw 언제 끝나?"),
                         (s.sid, "세션에 흔적 없는 입력")])
        ingest.run()
        con = dbm.open_db()
        kinds = [r[0] for r in con.execute("SELECT human_kind FROM cards ORDER BY seq")]
        self.assertEqual(kinds, ["human:typed", "goal", "slash"])
        self.assertEqual(con.execute("SELECT human FROM cards WHERE human_kind='goal'").fetchone()[0],
                         "Goal set: 끝까지 진행하고 보고해")
        self.assertEqual(con.execute("SELECT count(*) FROM cards WHERE delivered=0").fetchone()[0], 0)
        self.assertIn("사람 입력 3건 = 카드 3장 + 인자 없는 슬래시 명령 0건", views.day(con, "2026-09-30"))
        h = check.history_crosscheck(con)
        t = h["totals"]
        self.assertEqual(t.get("matched"), 3)
        # 제어 낱말(clear)이 다른 입력 줄에 있어도 '/goal clear' 는 누락이 아니라 제어다
        self.assertEqual((t.get("goal_control"), t.get("unmatched_btw"), t.get("unmatched_history_only")), (1, 1, 1))
        self.assertEqual(h["missed"], 0)
        # 감시가 동작하는지: goal 카드가 빠지면(고치기 전 모양) '누락' 으로 드러난다
        con.execute("DELETE FROM cards WHERE human_kind='goal'")
        h = check.history_crosscheck(con)
        self.assertEqual(h["missed"], 1)
        self.assertEqual(h["totals"].get("unmatched_missed"), 1)
        self.assertIn("끝까지", h["unmatched_examples"][0][1])
        con.close()

    def test_redaction_token_and_paste_wrapper_match(self):
        s = Sess()
        body = "\n\n<pasted_content id=\"604b\">\n%s\n</pasted_content id=\"604b\">\n\n jev 키" % FAKE_SECRET
        self.env.write_lines(self.env.session_path(s.sid), [s.human(body), s.assistant_text("네")])
        _hist(self.env, [(s.sid, FAKE_SECRET + " jev 키")])
        ingest.run()
        con = dbm.open_db()
        h = check.history_crosscheck(con)
        self.assertEqual(h["totals"].get("matched"), 1, h)
        self.assertEqual(h["missed"], 0)
        con.close()


class TestLateHarvestRetro(EnvCase):
    def test_late_value_redacted_in_stored_rows(self):
        s = Sess()
        p = self.env.session_path(s.sid)
        tu, tuid = s.tool_use("Bash", {"command": "echo %s > /tmp/x" % LATE})
        self.env.write_lines(p, [s.human("값은 %s 이다 기억해" % LATE), s.assistant_text("알겠다 %s" % LATE), tu,
                                 s.tool_result(tuid, LATE)])
        ingest.run()
        con = dbm.open_db()
        self.assertGreater(_db_hits(con, LATE), 0)  # 문맥 없이 적힌 값은 아직 모른다(가리지 않음)
        con.close()
        self.env.write_lines(p, [s.human("export PGPASSWORD=%s 로 접속" % LATE), s.assistant_text("접속했다")])
        r = ingest.run()
        self.assertGreaterEqual(r["retro"]["events_changed"], 3)
        self.assertEqual(r["harvest"]["strong_new"], 1)
        con = dbm.open_db()
        self.assertEqual(_db_hits(con, LATE), 0)
        self.assertEqual(con.execute("SELECT count(*) FROM fts WHERE fts MATCH ?", ('"%s"' % LATE,)).fetchone()[0], 0)
        out = views.resume(con, views.resolve_project(con, CWD)) + views.format_search(views.search(con, "기억해"), "기억해")
        self.assertNotIn(LATE, out)
        self.assertIn("[REDACTED:known]", out)
        con.close()
        self.assertEqual(leakscan.count_in_file(paths.db_path(), [LATE]), {})
        # 다음 실행: 새 값이 없으면 소급 가림을 하지 않는다
        r = ingest.run()
        self.assertIsNone(r["retro"])

    def test_secrets_local_change_retro(self):
        s = Sess()
        val = "Zz8localAdded!x"
        self.env.write_lines(self.env.session_path(s.sid), [s.human("새 값 %s 를 썼다" % val), s.assistant_text("응")])
        ingest.run()
        with open(os.path.join(self.env.home, "secrets.local"), "a") as fh:
            fh.write(val + "\n")
        r = ingest.run()
        self.assertGreaterEqual(r["retro"]["events_changed"], 1)
        con = dbm.open_db()
        self.assertEqual(_db_hits(con, val), 0)
        con.close()


class TestRederive(EnvCase):
    def _build(self):
        s = Sess()
        self.sid = s.sid
        lines = [s.human("첫 요청"), s.assistant_text("요약: 끝"),
                 s.base("user", message={"role": "user", "content": "/compact"}, promptId=uid())] \
            + _goal_midturn(s, "끝까지 진행하고 보고해")
        self.env.write_lines(self.env.session_path(s.sid), lines)
        ingest.run()
        con = dbm.open_db()
        # 옛 코드가 저장한 모양을 흉내 낸다: 분류(user_other), \u 변형 미가림, goal 카드 없음, 규칙 판 옛것
        var = FAKE_SECRET.replace("#", "\\u0023").replace("$", "\\u0024")
        key, raw = con.execute("SELECT key, raw FROM events WHERE kind='assistant' AND text LIKE '요약%'").fetchone()
        o = json.loads(raw)
        o["message"]["content"][0]["text"] = "요약: 값 " + var + " 끝"
        con.execute("UPDATE events SET raw=?, text=? WHERE key=?",
                    (json.dumps(o, ensure_ascii=False), "요약: 값 " + var + " 끝", key))
        con.execute("UPDATE events SET kind='user_other' WHERE kind='command'")
        con.execute("DELETE FROM cards WHERE human_kind='goal'")
        dbm.set_state(con, "derive_version", "old")
        con.close()
        return var

    def test_dry_run_then_apply(self):
        var = self._build()
        chk = Checker([FAKE_SECRET])
        s = rederive.run(apply_changes=False)
        self.assertEqual(s["stat"]["events_changed"], 2)
        self.assertEqual(s["kind_moves"], {"user_other/None -> command/None": 1})
        self.assertEqual(s["leak_in_changed"], {"before": 2, "after": 0})
        self.assertEqual(s["cards"]["delta_by_kind"], {"goal": 1})
        self.assertIn("미리보기", rederive.format_run(s))
        con = dbm.open_db()
        self.assertEqual(con.execute("SELECT count(*) FROM events WHERE raw LIKE ?", ("%" + var.replace("\\", "\\\\")
                                                                                     + "%",)).fetchone()[0], 1)
        con.close()
        r = ingest.run()
        self.assertTrue(any("rederive" in w for w in r["warnings"]), r["warnings"])
        s = rederive.run(apply_changes=True)
        self.assertTrue(s["applied"])
        con = dbm.open_db()
        self.assertEqual(_db_hits(con, var), 0)
        left = {}
        for (t,) in con.execute("SELECT raw FROM events"):
            chk.count_text(t, left, known_only=True)
        self.assertEqual(left, {})
        self.assertEqual(con.execute("SELECT count(*) FROM events WHERE kind='user_other'").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT count(*) FROM cards WHERE human_kind='goal'").fetchone()[0], 1)
        self.assertEqual(dbm.get_state(con, "derive_version"), rederive.DERIVE_VERSION)
        self.assertEqual(con.execute("SELECT count(*) FROM runs WHERE cmd='rederive --apply'").fetchone()[0], 1)
        con.close()
        self.assertEqual(leakscan.count_in_file(paths.db_path(), [FAKE_SECRET]), {})
        r = ingest.run()
        self.assertFalse(any("rederive" in w for w in r["warnings"]), r["warnings"])
        s = rederive.run(apply_changes=False)  # 멱등: 다시 보면 바뀔 행 0
        self.assertEqual(s["stat"].get("events_changed", 0), 0)
        self.assertEqual(s["cards"]["delta_by_kind"], {})

    def test_dry_run_does_not_write(self):
        self._build()
        with open(paths.db_path(), "rb") as fh:
            before = fh.read()
        rederive.run(apply_changes=False)
        with open(paths.db_path(), "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(os.path.getsize(paths.db_path() + "-wal") if os.path.exists(paths.db_path() + "-wal") else 0, 0)


class TestEncodedKvSeparator(unittest.TestCase):
    CASES = ["password\\u003d" + UESC, '{"cfg":"db_password\\u003d\\u0027%s\\u0027"}' % UESC,
             "PGPASSWORD%3D" + UESC, "password&#61;" + UESC, "api_key\\u003a" + UESC, "PGPASSWORD\\\\u003d" + UESC,
             "secret&#x3D;" + UESC]

    def test_redact_check_and_harvest(self):
        R = Redactor([])
        C = Checker([])
        for c in self.CASES:
            sc = leakscan.ShapeCounter()
            sc.text(c)
            self.assertEqual(sum(sc.c.values()), 1, c)  # 독립 형태 검사가 고치기 전 모양을 잡는다
            r = R.text(c)
            self.assertNotIn(UESC, r, c)
            left = {}
            C.count_text(r, left)
            self.assertEqual(left, {}, c)
            sc = leakscan.ShapeCounter()
            sc.text(r)
            self.assertEqual(sum(sc.c.values()), 0, c)
            found = {}
            R.harvest_text(c, found)
            H = Harvested(path=os.devnull)
            for v in found:
                H.add_raw(v)
            self.assertIn(UESC, H.strong, c)

    def test_escaped_chars_inside_value(self):
        """값 안의 \\u003d(=) 는 값의 일부로 본다(base64 끝 == 등). 닫는 \\u0027 따옴표에서 값이 끝난다."""
        v = "Ab12cd34ef=="
        for s, want in (("api_key\\u003d" + v.replace("=", "\\u003d"), "api_key\\u003d[REDACTED:pw_kv]"),
                        ("password\\u003d\\u0027" + v.replace("=", "\\u003d") + "\\u0027 x",
                         "password\\u003d\\u0027[REDACTED:pw_kv]\\u0027 x")):
            self.assertEqual(Redactor([]).text(s), want)


class TestHarvestShapes(EnvCase):
    # 모양 마스크(영숫자가 a·A·9 뿐)와 알파벳 열. 알파벳 열은 코드에 글자 그대로 쓰지 않고 만든다.
    MASKS = ["Aa9aA9aaAA#a", "aA9aaAa9", "AAAAaaaa99999aaAA", "".join(chr(c) for c in range(48, 58)) + "abcd"]

    def test_masks_not_harvested(self):
        for m in self.MASKS:
            self.assertIsNone(value_strength(m), m)
            self.assertFalse(leakscan.keep_value(m), m)
        self.assertEqual(value_strength(FAKE_SECRET), "strong")
        self.assertTrue(leakscan.keep_value(FAKE_SECRET))

    def test_existing_file_entries_kept_but_not_used(self):
        hp = paths.harvest_path()
        fd = os.open(hp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, ("# t\nv\t%s\nv\tQw3realLike99\n" % self.MASKS[0]).encode())
        os.close(fd)
        H = Harvested().load()
        self.assertEqual(H.active_strong(), ["Qw3realLike99"])
        H.add("Rt5another77x")
        H.save()
        with open(hp, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("v\t%s\n" % self.MASKS[0], body)  # 파일 내용은 지우지 않는다
        s = Sess()
        self.env.write_lines(self.env.session_path(s.sid), [s.human("모양 %s 과 password=%s" % (self.MASKS[0],
                                                                                             self.MASKS[1])),
                                                              s.assistant_text("ok")])
        r = ingest.run()
        self.assertEqual(r["harvest"]["strong_new"], 0)
        con = dbm.open_db()
        self.assertEqual(con.execute("SELECT count(*) FROM events WHERE text LIKE ?",
                                     ("%" + self.MASKS[0] + "%",)).fetchone()[0], 1)  # 마스크는 비밀이 아니라 남는다
        con.close()


class TestResumeNoMatch(EnvCase):
    def _sess(self, cmd, result):
        s = Sess()
        tu, tuid = s.tool_use("Bash", {"command": cmd})
        self.env.write_lines(self.env.session_path(s.sid), [s.human("찾아봐"), tu,
                                                            s.tool_result(tuid, result, is_error=True)])
        ingest.run()
        con = dbm.open_db()
        # 에러 절은 24시간 안의 에러만 내므로 기록 시각(2026-09-30 01시 UTC) 근처를 지금으로 둔다
        out = views.resume(con, views.resolve_project(con, CWD), now="2026-09-30T02:00:00.000Z")
        con.close()
        return out

    def test_grep_exit1_not_error(self):
        out = self._sess('echo "== kafka"; grep -rIl kafka . 2>/dev/null | head; ls docs | grep -i licen',
                         "Exit code 1\n== kafka")
        self.assertIn("## 해결 안 된 마지막 에러\n- 없음", out)

    def test_real_failure_still_error(self):
        out = self._sess("python3 run.py | grep ok", "Exit code 1\nTraceback (most recent call last):\nValueError: x")
        self.assertIn("도구 Bash", out)
        self.assertIn("ValueError", out)

    def test_rules(self):
        T, F = True, False
        for text, cmd, want in [("Exit code 1", "grep -r foo .", T), ("Exit code 1", "ls | grep x", T),
                                ("Exit code 1", "[ -f x ] && echo yes", T), ("Exit code 1", "git diff --quiet", T),
                                ("Exit code 1", "grep x f 2>/dev/null", T), ("Exit code 2", "grep x nofile", F),
                                ("Exit code 1", "grep x f > out.txt", F), ("Exit code 1", "grep x a; make build", F),
                                ("Exit code 1\ncd: no such file or directory: /n", "cd /n && grep x .", F),
                                ("Exit code 1", "cat > f <<EOF\nx\nEOF", F), ("Exit code 1", "set -e; grep x f", F),
                                ("Exit code 1", "sed -i s/a/b/ f | grep x", F)]:
            self.assertEqual(views.nomatch_exit(text, cmd), want, cmd)


class TestSearchDiversify(EnvCase):
    def test_per_session_cap_and_filters(self):
        a = Sess(t0="2026-09-30T01:00:00")
        b = Sess(t0="2026-09-20T01:00:00")
        la = []
        for i in range(6):
            la += [a.human("되풀이검색어 확인 %d" % i), a.assistant_text("응 %d" % i)]
        self.env.write_lines(self.env.session_path(a.sid), la)
        self.env.write_lines(self.env.session_path(b.sid), [b.human("원래 되풀이검색어 문제"), b.assistant_text("고침")])
        ingest.run()
        con = dbm.open_db()
        sids = [r["sid"] for r in views.search(con, "되풀이검색어", limit=4)["rows"]]
        self.assertEqual(sids.count(a.sid), 3)
        self.assertIn(b.sid, sids)
        sids = [r["sid"] for r in views.search(con, "되풀이검색어", limit=4, per_session=0)["rows"]]
        self.assertEqual(set(sids), {a.sid})
        sids = {r["sid"] for r in views.search(con, "되풀이검색어", exclude=[a.sid[:8]])["rows"]}
        self.assertEqual(sids, {b.sid})
        sids = {r["sid"] for r in views.search(con, "되풀이검색어", until="2026-09-25")["rows"]}
        self.assertEqual(sids, {b.sid})
        # 자리가 남으면 넘긴 행을 채운다
        self.assertEqual(len(views.search(con, "되풀이검색어", limit=20)["rows"]), 14)
        con.close()


if __name__ == "__main__":
    unittest.main()
