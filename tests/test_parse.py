import json
import unittest

from helpers import Sess, uid  # noqa: F401  (helpers 가 sys.path 를 맞춘다)
from pwikilib import parse


class TestLineTypes(unittest.TestCase):
    """조사에서 본 줄 type 18종 표본의 처리 정책과 분류."""

    def setUp(self):
        self.s = Sess()

    def samples(self):
        s = self.s
        return {
            "user": s.human("안녕"),
            "assistant": s.assistant_text("네"),
            "system": s.system("turn_duration", durationMs=1200, messageCount=3),
            "attachment": s.attachment({"type": "queued_command", "prompt": "그리고 이것도", "commandMode": "prompt",
                                        "origin": {"kind": "human"}}),
            "queue-operation": s.plain("queue-operation", operation="enqueue", timestamp="2026-09-30T01:00:00.000Z",
                                       content="작업 도중 입력"),
            "mode": s.plain("mode", mode="normal"),
            "last-prompt": s.plain("last-prompt", leafUuid=uid(), lastPrompt="x"),
            "atis-latch": s.plain("atis-latch", atis=""),
            "ai-title": s.plain("ai-title", aiTitle="세션 제목"),
            "permission-mode": s.plain("permission-mode", permissionMode="auto"),
            "bridge-session": s.plain("bridge-session", bridgeSessionId="cse_x", lastSequenceNum=0),
            "file-history-snapshot": {"type": "file-history-snapshot", "messageId": uid(), "snapshot": {},
                                      "isSnapshotUpdate": False},
            "frame-link": s.plain("frame-link", artifactCount=1, timestamp="2026-09-30T01:00:00.000Z", title="t"),
            "agent-setting": s.plain("agent-setting", agentSetting="general-purpose"),
            "file-history-delta": {"type": "file-history-delta", "messageId": uid(), "snapshotMessageId": uid(),
                                   "trackingPath": "/x", "backup": {}, "timestamp": "2026-09-30T01:00:00.000Z"},
            "cost-state": s.plain("cost-state", totalCostUSD=1.5, modelUsage={}),
            "artifact-autoreact-ledger": s.plain("artifact-autoreact-ledger", v=1, artifacts={}),
            "artifact-comment-monitor": s.plain("artifact-comment-monitor", v=1, artifacts={}),
        }

    def test_eighteen_types_policy(self):
        smp = self.samples()
        self.assertEqual(len(smp), 18)
        keep = {k: parse.line_policy(v) for k, v in smp.items()}
        for t in ("user", "assistant", "system", "attachment", "queue-operation", "mode", "ai-title", "permission-mode",
                  "frame-link", "agent-setting", "cost-state"):
            self.assertTrue(keep[t][0], t)
        for t, reason in (("last-prompt", "state:last-prompt"), ("atis-latch", "state:atis-latch"),
                          ("bridge-session", "state:bridge-session"), ("file-history-snapshot", "file_backup"),
                          ("file-history-delta", "file_backup"), ("artifact-autoreact-ledger", "state:artifact"),
                          ("artifact-comment-monitor", "state:artifact")):
            self.assertEqual(keep[t], (False, reason), t)

    def test_unknown_type_and_attachment(self):
        # 모르는 형식은 버리지 않고(원문은 30일 뒤 지워진다) 사유와 함께 유지한다.
        self.assertEqual(parse.line_policy({"type": "brand-new-thing"}), (True, "unknown_type:brand-new-thing"))
        self.assertEqual(parse.line_policy({"type": "attachment", "attachment": {"type": "mystery"}}),
                         (True, "unknown_attachment:mystery"))
        self.assertEqual(parse.line_policy({"type": "attachment", "attachment": {"type": "hook_success"}}),
                         (False, "hook_success"))
        self.assertEqual(parse.line_policy({"type": "system", "subtype": "stop_hook_summary"}), (False, "hook_summary"))

    def test_classify_human_forms(self):
        s = self.s
        self.assertEqual(parse.classify(s.human("타이핑"))[0], "human")
        self.assertEqual(parse.classify(s.human("대기열", source="queued"))[0], "human")
        img = s.human("", content=[{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAA"}}])
        self.assertEqual(parse.classify(img)[0], "human")
        intr = s.base("user", message={"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]})
        self.assertEqual(parse.classify(intr)[0], "interrupt")
        bash = s.base("user", message={"role": "user", "content": "<bash-input>ls -la</bash-input>"})
        self.assertEqual(parse.classify(bash)[0], "bash_in")
        sl = s.base("user", message={"role": "user", "content":
                                     "<command-name>/goal</command-name>\n<command-message>goal</command-message>\n"
                                     "<command-args>끝까지 해</command-args>"})
        self.assertEqual(parse.classify(sl)[0], "slash")
        sl0 = s.base("user", message={"role": "user", "content":
                                      "<command-name>/clear</command-name>\n<command-args></command-args>"})
        self.assertEqual(parse.classify(sl0)[0], "command")
        tn = s.base("user", message={"role": "user", "content": "<task-notification>\n<task-id>x</task-id>"},
                    origin={"kind": "task-notification"}, promptSource="system")
        self.assertEqual(parse.classify(tn)[0], "task_note")
        peer = s.base("user", message={"role": "user", "content": "Another Claude session sent a message:\n..."})
        self.assertEqual(parse.classify(peer)[0], "peer")
        meta = s.base("user", message={"role": "user", "content": "skill text"}, isMeta=True)
        self.assertEqual(parse.classify(meta)[0], "meta")
        sdk = s.base("user", message={"role": "user", "content": "자동 프롬프트"}, promptSource="sdk")
        self.assertEqual(parse.classify(sdk)[0], "sdk")
        side = s.base("user", message={"role": "user", "content": "[목표] 하위 작업"}, isSidechain=True)
        self.assertEqual(parse.classify(side)[0], "side_user")

    def test_queued_command_forms(self):
        s = self.s
        q = s.attachment({"type": "queued_command", "prompt": "이것도 해줘", "source_uuid": uid(), "commandMode": "prompt",
                          "origin": {"kind": "human"}})
        self.assertEqual(parse.classify(q), ("queued", "queued_command"))
        x = parse.extract(q, "queued", "queued_command")
        self.assertEqual(x["text"], "이것도 해줘")
        auto = s.attachment({"type": "queued_command", "prompt": "계속", "commandMode": "prompt",
                             "origin": {"kind": "auto-continuation"}})
        self.assertEqual(parse.classify(auto)[0], "att")
        tn = s.attachment({"type": "queued_command", "prompt": "<task-notification>...", "commandMode": "task-notification"})
        self.assertEqual(parse.classify(tn)[0], "att")
        qo = s.plain("queue-operation", operation="enqueue", content="사람 입력")
        self.assertEqual(parse.classify(qo), ("qo_human", "enqueue"))
        qo2 = s.plain("queue-operation", operation="enqueue", content="<task-notification>x</task-notification>")
        self.assertEqual(parse.classify(qo2), ("qo", "enqueue"))
        qo3 = s.plain("queue-operation", operation="remove")
        self.assertEqual(parse.classify(qo3), ("qo", "remove"))

    def test_compact_two_formats(self):
        md = ("This session is being continued...\n\nSummary:\n## 1. Primary Request and Intent\n\n사용자 요청\n\n"
              "1. 첫 항목\n2. 둘째 항목\n\n## 2. Key Technical Concepts\n- 개념\n\n## 3. Errors and fixes\n- 에러 A 고침\n")
        colon = ("This session is being continued...\n\nSummary:\n1. Primary Request and Intent:\n   요청 본문\n"
                 "   1. **본문 번호**: 무시돼야 함\n2. Key Technical Concepts:\n   - 개념\n"
                 "3. Files and Code Sections (주요):\n   - a.py\n4. Errors and fixes:\n   - 고침\n")
        a = parse.parse_compact_sections(md)
        b = parse.parse_compact_sections(colon)
        self.assertEqual([x["title"] for x in a], ["Primary Request and Intent", "Key Technical Concepts", "Errors and fixes"])
        self.assertTrue(all(x["fmt"] == "md" for x in a))
        self.assertIn("1. 첫 항목", a[0]["body"])
        self.assertEqual([x["title"] for x in b], ["Primary Request and Intent", "Key Technical Concepts",
                                                   "Files and Code Sections", "Errors and fixes"])
        self.assertTrue(all(x["fmt"] == "colon" for x in b))
        self.assertIn("본문 번호", b[0]["body"])

    def test_compact_line_extract(self):
        s = self.s
        o = s.base("user", message={"role": "user", "content": "Summary:\n## 1. Primary Request and Intent\n요청"},
                   isCompactSummary=True, isVisibleInTranscriptOnly=True)
        k, sub = parse.classify(o)
        self.assertEqual(k, "compact")
        x = parse.extract(o, k, sub)
        self.assertEqual(x["sections"][0]["title"], "Primary Request and Intent")

    def test_error_signature_normalization(self):
        a = parse.error_line("Exit code 1\nERROR: cannot open /tmp/a/b.txt at line 12")
        b = parse.error_line("Exit code 2\nERROR: cannot open /var/x.txt at line 999")
        self.assertTrue(a.startswith("exit: ERROR"))
        self.assertEqual(parse.error_sig(a), parse.error_sig(b))
        self.assertEqual(len(parse.error_sig(a)), 12)
        t1 = parse.error_line("Exit code 1\nTraceback (most recent call last):\n  File \"/a.py\", line 3\nKeyError: 'x'")
        t2 = parse.error_line("Exit code 1\nTraceback (most recent call last):\n  File \"/b.py\", line 9\nValueError: bad")
        self.assertEqual(t1, "exit: Traceback: KeyError: 'x'")
        self.assertNotEqual(parse.error_sig(t1), parse.error_sig(t2))
        c = parse.error_line("<tool_use_error>File has not been read yet</tool_use_error>")
        self.assertEqual(c, "File has not been read yet")
        self.assertNotEqual(parse.error_sig(a), parse.error_sig(c))

    def test_reduce_strips_images_and_copies(self):
        s = self.s
        big = "A" * 5000
        o = s.human("", content=[{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": big}},
                                 {"type": "text", "text": "캡처 봐줘"}])
        o2, tuid = s.tool_use("Read", {"file_path": "/x.png"})
        tr = s.tool_result(tuid, [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": big}}],
                           tur={"type": "image", "file": {"base64": big}, "filePath": "/x.png"})
        th = s.base("assistant", message={"id": "msg_1", "model": "m", "content": [{"type": "thinking", "thinking": "",
                                                                                    "signature": "S" * 300}]})
        strips = {}
        r1 = parse.reduce_line(o, strips)
        r2 = parse.reduce_line(tr, strips)
        r3 = parse.reduce_line(th, strips)
        self.assertNotIn(big, json.dumps(r1))
        self.assertNotIn(big, json.dumps(r2))
        self.assertIn("5000바이트", json.dumps(r1, ensure_ascii=False))
        self.assertEqual(r2["toolUseResult"], {"type": "image", "filePath": "/x.png"})
        self.assertNotIn("signature", json.dumps(r3))
        self.assertEqual(strips["image_base64"][0], 2)
        self.assertIn("toolUseResult_copy", strips)
        self.assertIn("thinking_signature", strips)
        self.assertIn("캡처 봐줘", parse.user_text(r1["message"]["content"]))

    def test_tool_extract(self):
        s = self.s
        o, tuid = s.tool_use("Write", {"file_path": "/a/b.py", "content": "print(1)"})
        k, sub = parse.classify(o)
        x = parse.extract(o, k, sub)
        self.assertEqual(x["tool_uses"][0]["fpath"], "/a/b.py")
        o, tuid = s.tool_use("Bash", {"command": "ls -la /tmp\necho hi", "description": "목록"})
        x = parse.extract(o, *parse.classify(o))
        self.assertEqual(x["tool_uses"][0]["cmd"], "ls -la /tmp")
        tr = s.tool_result(tuid, "The user doesn't want to proceed with this tool use.", is_error=True,
                           toolDenialKind="user-rejected")
        x = parse.extract(tr, *parse.classify(tr))
        self.assertEqual(x["tool_results"][0]["denial"], "user-rejected")
        self.assertEqual(x["tool_results"][0]["is_err"], 1)


if __name__ == "__main__":
    unittest.main()
