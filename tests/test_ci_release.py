"""tools/ci_release.py: 릴리즈 노트 추출(태그·VERSION 대조)과 노션 페이로드 조립. 네트워크는 쓰지 않는다."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

import helpers  # noqa: F401  (저장소 뿌리를 sys.path 에 넣는다)

sys.path.insert(0, os.path.join(helpers.ROOT, "tools"))
import ci_release  # noqa: E402

CL = """# Changelog

## [0.3.0] - 2026-10-20

### Added
- 셋째

## [0.2.0] - 2026-10-06
- 둘째
"""
INIT = 'VERSION = "0.3.0"\n'
TOOL = os.path.join(helpers.ROOT, "tools", "ci_release.py")


class NotesTest(unittest.TestCase):
    def test_section_body_without_heading(self):
        notes, err = ci_release.release_notes("v0.3.0", INIT, CL)
        self.assertIsNone(err)
        self.assertEqual(notes, "### Added\n- 셋째\n")
        self.assertNotIn("0.2.0", notes)

    def test_last_section(self):
        self.assertEqual(ci_release.changelog_section(CL, "0.2.0"), "- 둘째\n")

    def test_tag_version_mismatch(self):
        notes, err = ci_release.release_notes("v0.2.0", INIT, CL)
        self.assertIsNone(notes)
        self.assertIn("VERSION", err)

    def test_bad_tag_shape(self):
        for tag in ("0.3.0", "v", "v0.3.0-rc1", "main"):
            self.assertIsNotNone(ci_release.release_notes(tag, INIT, CL)[1], tag)

    def test_missing_section(self):
        self.assertIsNotNone(ci_release.release_notes("v0.4.0", 'VERSION = "0.4.0"\n', CL)[1])

    def test_repo_changelog_has_current_version(self):
        with open(os.path.join(helpers.ROOT, "pwikilib", "__init__.py"), encoding="utf-8") as fh:
            init_text = fh.read()
        with open(os.path.join(helpers.ROOT, "CHANGELOG.md"), encoding="utf-8") as fh:
            cl = fh.read()
        ver = ci_release._VER_RX.search(init_text).group(1)
        notes, err = ci_release.release_notes("v" + ver, init_text, cl)
        self.assertIsNone(err)


class NotionPayloadTest(unittest.TestCase):
    HOSTILE = 'fix: "quoted" $(touch /tmp/x) `id` \\ back\nsecond line\t탭'

    def env(self, **kw):
        e = {"NOTION_DATABASE_ID": "db123", "REPO": "o/pwiki", "REF": "v0.3.0",
             "RUN_URL": "https://github.com/o/pwiki/actions/runs/1", "ACTOR": "dev", "COMMIT_MSG": self.HOSTILE}
        e.update(kw)
        return e

    def test_hostile_commit_message_survives_as_text(self):
        p = ci_release.notion_payload(self.env())
        dumped = json.dumps(p, ensure_ascii=False)
        self.assertEqual(json.loads(dumped), p)
        commit = p["children"][2]["bulleted_list_item"]["rich_text"][0]["text"]["content"]
        self.assertEqual(commit, '커밋: fix: "quoted" $(touch /tmp/x) `id` \\ back second line 탭')

    def test_properties(self):
        props = ci_release.notion_payload(self.env())["properties"]
        self.assertEqual(props["이슈명"]["title"][0]["text"]["content"], "빌드/릴리즈 실패: o/pwiki (v0.3.0)")
        self.assertEqual(props["실패링크"]["url"], "https://github.com/o/pwiki/actions/runs/1")
        self.assertEqual(props["상태"]["status"]["name"], "진행 전")

    def test_long_summary_is_chunked_under_limit(self):
        p = ci_release.notion_payload(self.env(), summary="x" * 4500 + "END")
        code = p["children"][-1]["code"]["rich_text"]
        self.assertEqual(len(code), 3)
        self.assertTrue(all(len(t["text"]["content"]) <= ci_release.TEXT_LIMIT for t in code))
        self.assertTrue(code[-1]["text"]["content"].endswith("END"))

    def test_no_summary_no_code_block(self):
        p = ci_release.notion_payload(self.env(), summary="  \n")
        self.assertFalse(any(c["type"] == "code" for c in p["children"]))

    def test_empty_run_url_has_no_link(self):
        p = ci_release.notion_payload(self.env(RUN_URL=""))
        self.assertIsNone(p["properties"]["실패링크"]["url"])
        self.assertNotIn("link", p["children"][3]["bulleted_list_item"]["rich_text"][1]["text"])

    def test_cli_reads_env_and_summary_file(self):
        fd, path = tempfile.mkstemp(prefix="pwiki-test-", suffix=".txt")
        self.addCleanup(os.remove, path)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("job test / step Run tests: failure\nAssertionError: 'a' != \"b\"\n")
        env = dict(os.environ, SUMMARY_FILE=path, **self.env())
        out = subprocess.run([sys.executable, TOOL, "notion-payload"], env=env, check=True,
                             stdout=subprocess.PIPE).stdout
        p = json.loads(out.decode("utf-8"))
        self.assertIn('AssertionError', p["children"][-1]["code"]["rich_text"][0]["text"]["content"])

    def test_cli_usage_error(self):
        r = subprocess.run([sys.executable, TOOL], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
