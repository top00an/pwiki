"""독립 검증 4차(보안·가림, 2026-10-03) 지적 재현 시험.
가짜 값은 고정 씨앗으로 실행 중에 만든다. 값 글자가 코드·대화 기록의 비밀번호 문맥에 남지 않게 한다
(시험 파일의 문맥 리터럴이 실제 수집에서 수확되던 일을 막는다)."""
import json
import os
import random
import string
import unittest
import urllib.parse

from helpers import Env, Sess
from pwikilib import check, ingest, leakscan, paths, rederive
from pwikilib import db as dbm
from pwikilib.redact import Checker, Harvested, Redactor

PW = "pass" + "word"


def fake(rnd, n=16, sp="#&!%+"):
    """가짜 값: 영문 대소문자·숫자에 기호 2개(앞뒤 3글자 안쪽)."""
    while True:
        s = [rnd.choice(string.ascii_letters + string.digits) for _ in range(n)]
        for i in rnd.sample(range(3, n - 3), 2):
            s[i] = rnd.choice(sp)
        v = "".join(s)
        if any(c.isdigit() for c in v) and any(c.isupper() for c in v) and any(c.islower() for c in v):
            return v


def esc_u(v, bs=1):
    return "".join(c if c.isalnum() else "\\" * bs + "u%04x" % ord(c) for c in v)


def html_named(v):
    return "".join({"&": "&amp;", '"': "&quot;", "<": "&lt;", ">": "&gt;", "'": "&apos;"}.get(c, c) for c in v)


def html_dec(v):
    return "".join(c if c.isalnum() else "&#%d;" % ord(c) for c in v)


def forms(v):
    q = urllib.parse.quote
    return {"plain": v, "u_esc": esc_u(v), "u_esc2": esc_u(v, 2), "url": q(v, safe=""),
            "url2": q(q(v, safe=""), safe=""), "html_dec": html_dec(v), "html_named": html_named(v),
            "html_named2": html_named(html_named(v)), "html_amp_dec": html_named(html_dec(v)),
            "shell": "".join("\\" + c if not c.isalnum() else c for c in v), "json2": json.dumps(json.dumps(v))[1:-1]}


# 문맥 키값 모양(%s 자리에 값). 고치기 전 코드가 값을 가리지도 수확하지도 않던 모양과 HTML 따옴표 변형.
KV_SHAPES = {
    "arrow": PW + " => %s",
    "arrow_q": "'" + PW + "' => '%s'",
    "html_quot": PW + "=&quot;%s&quot;",
    "html_34": PW + "=&#34;%s&#34;",
    "html_39": PW + ": &#39;%s&#39;",
    "pct253D": PW + "%%253D%s",
    "html_amp61": PW + "&amp;#61;%s",
    "u0020_sep": PW + "\\u0020=\\u0020%s",
    "env_pct253D": "PG" + PW.upper() + "%%253D%s",
}


def _with_amp(rnd):
    v = fake(rnd)
    while "&" not in v:
        v = fake(rnd)
    return v


class TestKnownVariantsIndependent(unittest.TestCase):
    """지적 1: 알려진 값의 두 번 URL 인코딩·HTML 이름 엔티티(&amp;) 모양을 가림이 놓치고, 자체 검사도 같은 정규식이라 놓쳤다."""

    def test_variants_counted_and_redacted(self):
        rnd = random.Random(20261003)
        for _ in range(12):
            k = _with_amp(rnd)
            R = Redactor([k])
            C = Checker([k])
            for name, f in forms(k).items():
                text = "앞 " + f + " 뒤"
                # 독립 대조(leakscan)는 가리기 전 글에서 값을 센다
                self.assertEqual(list(leakscan.count_in_bytes(text, [k]).values()), [1], name)
                # 자체 검사(Checker)도 센다
                cc = {}
                C.count_text(text, cc)
                self.assertGreater(cc.get("known", 0), 0, name)
                # 가림 뒤에는 독립 대조가 0
                out = R.text(text)
                self.assertEqual(sum(leakscan.count_in_bytes(out, [k]).values()), 0, name)
                self.assertTrue("[REDACTED:known]" in out, name)

    def test_no_false_hits_on_other_values(self):
        """정규화 대조가 비슷한 다른 값까지 세지 않는다."""
        rnd = random.Random(7)
        k = _with_amp(rnd)
        other = k[:-2] + ("Q" if k[-2] != "Q" else "R") + k[-1]
        for name, f in forms(other).items():
            self.assertEqual(sum(leakscan.count_in_bytes("앞 " + f + " 뒤", [k]).values()), 0, name)


class EnvCase(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()


class TestRedactCheckSeesKnownVariants(EnvCase):
    """지적 1 고친 모양: 가림을 끈 행(변형 모양 알려진 값)을 넣으면 redact-check 의 독립 대조가 0 이 아니다."""

    def test_unredacted_variant_rows_fail_check(self):
        rnd = random.Random(31)
        k = _with_amp(rnd)
        with open(os.path.join(self.env.home, "secrets.local"), "a") as fh:
            fh.write(k + "\n")
        s = Sess()
        self.env.write_lines(self.env.session_path(s.sid), [s.human("처음"), s.assistant_text("응답")])
        ingest.run()
        con = dbm.open_db()
        res = check.redact_check(con, vault=self.env.vault, include_logs=False)
        self.assertEqual(res["total_hits"], 0, check.format_redact_check(res))
        key = con.execute("SELECT key FROM events WHERE kind='assistant'").fetchone()[0]
        for name in ("url2", "html_named", "html_named2", "html_amp_dec", "u_esc2"):
            f = forms(k)[name]
            con.execute("UPDATE events SET text=? WHERE key=?", ("가림 끈 행 " + f + " 끝", key))
            res = check.redact_check(con, vault=self.env.vault, include_logs=False)
            ind = res["locations"]["ind_source_values"]
            self.assertGreater(sum(ind["hits"].values()), 0, name)
            self.assertGreater(res["total_hits"], 0, name)
            # 알려진 값은 모양(마스크)을 출력하지 않는다
            self.assertFalse(leakscan.mask(k) in check.format_redact_check(res), name)
        con.close()


class TestKvShapes(unittest.TestCase):
    """지적 2: '=>', &quot;·&#34;·&#39; 따옴표, %253D, &amp;#61;, \\u0020 구분자 모양."""

    def test_redact_and_harvest(self):
        rnd = random.Random(99)
        for name, tpl in KV_SHAPES.items():
            v = fake(rnd, 14, sp="!#+")
            text = "설정 " + (tpl % v) + " 끝"
            R = Redactor([])
            out = R.text(text)
            self.assertFalse(v in out, name)
            self.assertFalse(v[2:-2] in out, name)
            found = {}
            R.harvest_text(text, found)
            H = Harvested(path=os.devnull)
            for x in found:
                H.add_raw(x)
            self.assertTrue(v in H.strong, name)
            # 독립 검사: 원천 문맥 값 추출과 형태 검사가 가리기 전 모양을 잡고, 가린 뒤 모양은 0
            lf = {}
            leakscan._scan_text(text, lf)
            self.assertTrue(v in lf, name)
            sc = leakscan.ShapeCounter()
            sc.text(text)
            self.assertGreater(sum(sc.c.values()), 0, name)
            sc = leakscan.ShapeCounter()
            sc.text(out)
            self.assertEqual(sum(sc.c.values()), 0, name)
            left = {}
            Checker([]).count_text(out, left)
            self.assertEqual(left, {}, name)

    def test_redact_check_counts_unredacted_kv(self):
        """원천에 있던 키값 모양이 가려지지 않고 남으면 독립 대조가 센다(형태 검사·원천 값 대조)."""
        env = Env()
        try:
            rnd = random.Random(5)
            vals = {}
            s = Sess()
            lines = []
            for name, tpl in KV_SHAPES.items():
                v = fake(rnd, 14, sp="!#+")
                vals[name] = v
                lines.append(s.human("설정 " + (tpl % v) + " 끝"))
            env.write_lines(env.session_path(s.sid), lines + [s.assistant_text("응답")])
            found, _ = leakscan.extract_source_values()
            for name, v in vals.items():
                self.assertTrue(v in found, name)
            ingest.run()
            con = dbm.open_db()
            res = check.redact_check(con, vault=env.vault, include_logs=False)
            self.assertEqual(res["total_hits"], 0, check.format_redact_check(res))
            # 가림을 끈 행: 원래 모양을 그대로 넣으면 독립 검사가 0 이 아니다
            key = con.execute("SELECT key FROM events WHERE kind='assistant'").fetchone()[0]
            for name, tpl in KV_SHAPES.items():
                con.execute("UPDATE events SET text=? WHERE key=?", ("남은 " + (tpl % vals[name]) + " 끝", key))
                res = check.redact_check(con, vault=env.vault, include_logs=False)
                loc = res["locations"]
                self.assertGreater(sum(loc["ind_source_values"]["hits"].values()), 0, name)
                self.assertGreater(sum(loc["ind_shape_db"]["hits"].values()), 0, name)
            con.close()
        finally:
            env.close()


class TestStoredRowHarvest(EnvCase):
    """지적 3: 규칙이 바뀌어 새로 문맥으로 잡히는 값은 저장 행에서 수확해야 문맥 없는 사본까지 가린다."""

    def _build(self):
        rnd = random.Random(4242)
        self.v = fake(rnd, 15, sp="!#+")
        s = Sess()
        self.sid = s.sid
        self.env.write_lines(self.env.session_path(s.sid), [
            s.human("첫 요청"), s.assistant_text("자리 A"), s.human("두 번째"), s.assistant_text("자리 B")])
        mem = os.path.join(self.env.claude, "projects", "-Users-tester-work-demo", "memory")
        os.makedirs(mem, exist_ok=True)
        with open(os.path.join(mem, "MEMORY.md"), "w") as fh:
            fh.write("# 메모\n- 자리 C\n")
        ingest.run()
        con = dbm.open_db()
        # 옛 코드가 저장한 모양을 흉내 낸다: 지금 규칙의 문맥(= 구분자)이 가려지지 않은 행 하나,
        # 같은 값이 문맥 없이 적힌 행 하나와 문서 하나. 수확 목록에는 값이 없다.
        ctx = "접속 PG" + PW.upper() + "\\u003d" + self.v + " 로 붙음"
        bare = "다시 쓴 값 " + self.v + " 기억"
        for marker, new in (("자리 A", ctx), ("자리 B", bare)):
            key, raw = con.execute("SELECT key, raw FROM events WHERE text=?", (marker,)).fetchone()
            o = json.loads(raw)
            o["message"]["content"][0]["text"] = new
            con.execute("UPDATE events SET raw=?, text=? WHERE key=?", (json.dumps(o, ensure_ascii=False), new, key))
        p = con.execute("SELECT path FROM docs WHERE text LIKE '%자리 C%'").fetchone()[0]
        con.execute("UPDATE docs SET text=? WHERE path=?", ("# 메모\n- 값 " + self.v + " 보관\n", p))
        con.execute("DELETE FROM state")
        con.close()

    def _hits(self):
        con = dbm.open_db()
        n = 0
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            if t == "fts":
                continue
            cols = [r[1] for r in con.execute('PRAGMA table_info("%s")' % t)]
            for row in con.execute('SELECT %s FROM "%s"' % (", ".join('"%s"' % c for c in cols), t)):
                n += sum(1 for x in row if isinstance(x, str) and self.v in x)
        con.close()
        return n

    def _harvested(self):
        return self.v in Harvested().load().strong

    def test_rederive_preview_and_apply(self):
        self._build()
        self.assertEqual(self._hits(), 5)
        s = rederive.run(apply_changes=False)
        self.assertEqual(s["harvest"]["strong_new"], 1)
        self.assertGreaterEqual(s["stat"]["events_changed"], 2)
        self.assertEqual(s["stat"]["docs_changed"], 1)
        self.assertEqual(s["leak_in_changed"]["after"], 0)
        self.assertGreater(s["leak_in_changed"]["before"], 0)
        self.assertIn("수확", rederive.format_run(s))
        self.assertEqual(self._hits(), 5)  # 미리보기는 쓰지 않는다
        self.assertFalse(self._harvested())
        s = rederive.run(apply_changes=True)
        self.assertEqual(self._hits(), 0)
        self.assertTrue(self._harvested())
        self.assertEqual(sum(leakscan.count_in_file(paths.db_path(), [self.v]).values()), 0)
        con = dbm.open_db()
        self.assertEqual(dbm.get_state(con, "stored_harvest_version"), rederive.DERIVE_VERSION)
        con.close()
        s = rederive.run(apply_changes=False)  # 멱등
        self.assertEqual(s["stat"].get("events_changed", 0), 0)
        self.assertEqual(s["harvest"]["strong_new"], 0)

    def test_first_ingest_after_rule_change(self):
        self._build()
        r = ingest.run()
        self.assertEqual(r["stored_harvest"]["strong_new"], 1)
        self.assertEqual(self._hits(), 0)
        self.assertTrue(self._harvested())
        self.assertEqual(sum(leakscan.count_in_file(paths.db_path(), [self.v]).values()), 0)
        r = ingest.run()  # 같은 규칙 판에서는 저장 행 수확을 다시 하지 않는다
        self.assertIsNone(r["stored_harvest"])

    def test_found_again_after_interrupted_retro(self):
        """수확 파일은 저장됐는데 소급 가림 전에 멈춘 경우: 다음 실행이 같은 값을 다시 찾아 소급한다(새 값이 아니어도)."""
        self._build()
        h = Harvested().load()
        h.add(self.v)
        h.save()
        r = ingest.run()
        self.assertEqual(r["stored_harvest"]["strong_new"], 0)
        self.assertEqual(r["stored_harvest"]["retro_values"], 1)
        self.assertEqual(self._hits(), 0)


if __name__ == "__main__":
    unittest.main()
