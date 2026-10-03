"""데이터 자리(PWIKI_HOME·vault) 안전 시험: 사람 파일이 든 폴더는 설치가 멈추고, purge 는 pwiki 가 만든 것만 지운다.

검토 2회차 재현(PWIKI_HOME=Documents 를 통째로 지움, days/·projects/ 의 사람 파일을 지움, 원래 _notes 가 있던
사람 vault 를 pwiki vault 로 오인)과 --help 가 코드 줄까지 찍던 결함의 회귀 시험."""
import os
import sqlite3
import unittest

from test_dist_install import DistBase, INSTALL_SH, MAKE_DIST, UNINSTALL_SH


def write(p, text="사람 파일\n"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)


def listing(top):
    out = []
    for dp, dn, fn in os.walk(top):
        out += [os.path.relpath(os.path.join(dp, f), top) for f in fn]
    return sorted(out)


class DataPlaceTest(DistBase):
    def test_home_with_user_files_is_refused(self):
        self.fixture()
        docs = os.path.join(self.home, "Documents")
        write(os.path.join(docs, "계약서.txt"))
        before = listing(docs)
        rc, out = self.install("--no-collector", "--no-hook", "--yes", PWIKI_HOME=docs)
        self.assertEqual(rc, 2, out)
        self.assertIn("PWIKI_HOME 자리", out)
        self.assertIn("pwiki 가 아닌 항목이 1개", out)
        self.assertEqual(listing(docs), before)
        self.assertFalse(os.path.exists(self.vault))
        # 숨김 파일만 있는 폴더는 받는다
        hid = os.path.join(self.home, "data-hidden")
        write(os.path.join(hid, ".DS_Store"), "x")
        rc, out = self.install("--no-collector", "--no-hook", "--yes", PWIKI_HOME=hid)
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(hid, ".pwiki-home")))

    def test_purge_home_keeps_user_files(self):
        self.fixture()
        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.pwiki_home, ".pwiki-home")))
        write(os.path.join(self.pwiki_home, "내 메모.txt"))
        write(os.path.join(self.pwiki_home, "logs", "내 기록.txt"))
        rc, out = self.uninstall("--purge-data", "--yes", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("그 밖의 파일 2개는 남긴다", out)
        self.assertTrue(os.path.isfile(os.path.join(self.pwiki_home, "pwiki.db")))
        rc, out = self.uninstall("--purge-data", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("rm -rf", out)
        self.assertEqual(listing(self.pwiki_home), ["logs/내 기록.txt", "내 메모.txt"])
        self.assertEqual(sorted(os.listdir(self.pwiki_home)), ["logs", "내 메모.txt"])
        self.assertIn("pwiki 가 만들지 않은 파일 2개", out)

    def test_purge_home_without_mark_with_user_files_deletes_nothing(self):
        self.fixture()
        docs = os.path.join(self.home, "Documents")
        write(os.path.join(docs, "계약서.txt"))
        write(os.path.join(docs, "pwiki.db"), "x")  # 우연히 같은 이름이 있어도 표시가 없으면 사람 폴더다
        rc, out = self.uninstall("--purge-data", "--yes", PWIKI_HOME=docs)
        self.assertEqual(rc, 0, out)
        self.assertIn("사람 폴더로 본다", out)
        self.assertEqual(listing(docs), ["pwiki.db", "계약서.txt"])

    def test_vault_with_existing_notes_is_refused(self):
        self.fixture()
        v = os.path.join(self.home, "Vault")
        write(os.path.join(v, "_notes", "idea.md"))
        write(os.path.join(v, "projects", "plan.md"))
        rc, out = self.install("--no-collector", "--no-hook", "--yes", PWIKI_VAULT=v)
        self.assertEqual(rc, 2, out)
        self.assertIn("vault 자리", out)
        self.assertFalse(os.path.exists(self.pwiki_home))
        self.assertEqual(listing(v), ["_notes/idea.md", "projects/plan.md"])
        # 제거기도 표시 없는 사람 vault 는 건드리지 않는다
        rc, out = self.uninstall("--purge-data", "--yes", PWIKI_VAULT=v)
        self.assertEqual(rc, 0, out)
        self.assertIn("사람 폴더로 본다", out)
        self.assertEqual(listing(v), ["_notes/idea.md", "projects/plan.md"])

    def test_purge_vault_keeps_user_files_inside_page_folders(self):
        self.fixture()
        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.vault, ".pwiki-vault")))
        pages = [r for r in listing(self.vault) if r.split("/")[0] in ("days", "projects", "cards")]
        self.assertTrue(pages)
        edited = os.path.join(self.vault, pages[0])
        with open(edited, "a", encoding="utf-8") as fh:
            fh.write("\n사람이 고친 줄\n")
        mine = ["projects/내-계획.md", "days/사진/a.png", "내노트.md", "_notes/메모.md"]
        for rel in mine:
            write(os.path.join(self.vault, rel))
        rc, out = self.uninstall("--purge-data", "--yes", "--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("사람이 고친 페이지 1개 포함", out)
        self.assertIn("pwiki 가 만들지 않은 파일 4개는 남긴다", out)
        rc, out = self.uninstall("--purge-data", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(listing(self.vault), sorted(mine))
        self.assertIn("pwiki 가 만들지 않은 파일 4개(숨김 0개 포함)", out)
        self.assertFalse(os.path.exists(os.path.join(self.vault, "cards")), "빈 폴더는 지운다")
        self.assertFalse(os.path.exists(self.pwiki_home))

    def test_install_before_marks_is_still_recognized(self):
        """표시 파일이 생기기 전 설치(DB·export 기록은 있다)는 다시 설치하고 지울 수 있다."""
        self.fixture()
        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 0, out)
        os.unlink(os.path.join(self.pwiki_home, ".pwiki-home"))
        os.unlink(os.path.join(self.vault, ".pwiki-vault"))
        write(os.path.join(self.vault, "_notes", "메모.md"))
        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 0, out)
        os.unlink(os.path.join(self.pwiki_home, ".pwiki-home"))
        os.unlink(os.path.join(self.vault, ".pwiki-vault"))
        rc, out = self.uninstall("--purge-data", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(self.pwiki_home))
        self.assertEqual(listing(self.vault), ["_notes/메모.md"])

    def test_legacy_home_with_user_backup_folders(self):
        """표시 파일 이전 설치 곁에 사람이 백업 폴더를 둔 모양: pwiki DB(표 구성 확인)로 기존 설치로 알아본다.
        재설치는 같은 DB 를 이어 쓰고, 제거는 pwiki 파일만 지우고 백업 폴더는 그대로 둔다."""
        self.fixture()
        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 0, out)
        for p in (os.path.join(self.pwiki_home, ".pwiki-home"), os.path.join(self.pwiki_home, "install.json"),
                  os.path.join(self.vault, ".pwiki-vault")):
            if os.path.exists(p):
                os.unlink(p)
        mine = ["old_a/pwiki.db", "old_a/note.txt", "old_b/logs/ingest.log"]
        for rel in mine:
            write(os.path.join(self.pwiki_home, rel))
        db = os.path.join(self.pwiki_home, "pwiki.db")
        ino = os.stat(db).st_ino
        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIn("기존 pwiki 설치", out)
        self.assertIn("다른 항목 2개는 그대로 둔다", out)
        self.assertEqual(os.stat(db).st_ino, ino)
        self.assertEqual([x for x in listing(self.pwiki_home) if x.startswith("old_")], sorted(mine))
        os.unlink(os.path.join(self.pwiki_home, ".pwiki-home"))
        rc, out = self.uninstall("--purge-data", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(listing(self.pwiki_home), sorted(mine))
        self.assertFalse(os.path.exists(db))

    def test_user_logs_runs_folders_are_not_taken_for_pwiki(self):
        """DB·표시가 없는 자리의 사람 logs/·runs/ 폴더는 pwiki 것으로 보지 않는다. pwiki 로그 이름만 든 logs/ 는 받는다."""
        self.fixture()
        work = os.path.join(self.home, "Work")
        write(os.path.join(work, "logs", "diary.txt"))
        write(os.path.join(work, "runs", "r1.csv"))
        before = listing(work)
        rc, out = self.install("--no-collector", "--no-hook", "--yes", PWIKI_HOME=work)
        self.assertEqual(rc, 2, out)
        self.assertIn("pwiki 가 아닌 항목이 2개", out)
        rc, out = self.uninstall("--purge-data", "--yes", PWIKI_HOME=work)
        self.assertEqual(rc, 0, out)
        self.assertIn("사람 폴더로 본다", out)
        self.assertEqual(listing(work), before)
        # 다른 프로그램의 pwiki.db(표 구성이 다름)도 사람 파일로 본다
        other = os.path.join(self.home, "Other")
        os.makedirs(other)
        con = sqlite3.connect(os.path.join(other, "pwiki.db"))
        con.execute("CREATE TABLE notes(x)")
        con.commit()
        con.close()
        rc, out = self.install("--no-collector", "--no-hook", "--yes", PWIKI_HOME=other)
        self.assertEqual(rc, 2, out)
        self.assertIn("pwiki 가 아닌 항목이 1개", out)
        # pwiki 로그 이름만 든 logs/ 와 미리 둔 secrets.local 은 받는다
        pre = os.path.join(self.home, "Pre")
        write(os.path.join(pre, "logs", "ingest.log"), "")
        write(os.path.join(pre, "secrets.local"), "# 주석\n")
        rc, out = self.install("--no-collector", "--no-hook", "--yes", PWIKI_HOME=pre)
        self.assertEqual(rc, 0, out)

    def test_export_record_outside_page_folders_is_not_deleted(self):
        self.fixture()
        rc, out = self.install("--no-collector", "--no-hook", "--yes")
        self.assertEqual(rc, 0, out)
        write(os.path.join(self.vault, "_notes", "메모.md"))
        write(os.path.join(self.home, "outside.md"))
        con = sqlite3.connect(os.path.join(self.pwiki_home, "pwiki.db"))
        with con:
            for rel in ("_notes/메모.md", "../outside.md", "/etc/hosts"):
                con.execute("INSERT OR REPLACE INTO exports(path, sha1, bytes, at) VALUES (?,?,?,?)", (rel, "0", 0, "x"))
        con.close()
        rc, out = self.uninstall("--purge-data", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.isfile(os.path.join(self.vault, "_notes", "메모.md")))
        self.assertTrue(os.path.isfile(os.path.join(self.home, "outside.md")))


class HelpTextTest(DistBase):
    def test_help_prints_only_the_comment_block(self):
        for args in ([INSTALL_SH, "--help"], [UNINSTALL_SH, "--help"], [MAKE_DIST]):
            rc, out = self.sh(["/bin/bash"] + args)
            self.assertIn(rc, (0, 2), out)
            self.assertIn("종료 코드", out)
            self.assertNotIn("set -", out)
            self.assertFalse([ln for ln in out.splitlines() if ln.startswith("#")], out)


if __name__ == "__main__":
    unittest.main()
