"""시험 공용 도구. 실제 ~/.claude 는 건드리지 않고 임시 폴더에 가짜 기록을 만든다.
가짜 비밀값만 쓴다(실제 비밀값은 시험에 넣지 않는다)."""
import json
import os
import shutil
import sys
import tempfile
import uuid as uuidm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAKE_SECRET = "Zq7fakeSecret9#$"
PROJ = "-Users-tester-work-demo"
CWD = "/Users/tester/work/demo"


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="pwiki-test-")
        self.claude = os.path.join(self.tmp, "claude")
        self.home = os.path.join(self.tmp, "home")
        self.vault = os.path.join(self.tmp, "vault")
        self.codex = os.path.join(self.tmp, "codex")  # 만들지 않는다(실제 ~/.codex 를 읽지 않게 가리키기만)
        os.makedirs(os.path.join(self.claude, "projects", PROJ), exist_ok=True)
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.old = {k: os.environ.get(k) for k in ("PWIKI_CLAUDE_DIR", "PWIKI_HOME", "PWIKI_VAULT",
                                                  "PWIKI_CODEX_DIR")}
        os.environ["PWIKI_CLAUDE_DIR"] = self.claude
        os.environ["PWIKI_HOME"] = self.home
        os.environ["PWIKI_VAULT"] = self.vault
        os.environ["PWIKI_CODEX_DIR"] = self.codex
        sp = os.path.join(self.home, "secrets.local")
        fd = os.open(sp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.write(fd, ("# test\n" + FAKE_SECRET + "\n").encode())
        os.close(fd)

    def session_path(self, sid, proj=PROJ):
        return os.path.join(self.claude, "projects", proj, sid + ".jsonl")

    def write_lines(self, path, objs, mode="a", trailing_newline=True):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, mode, encoding="utf-8") as fh:
            for i, o in enumerate(objs):
                s = o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)
                fh.write(s)
                if trailing_newline or i < len(objs) - 1:
                    fh.write("\n")

    def close(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


def uid():
    return str(uuidm.uuid4())


class Sess:
    """한 세션의 줄을 만든다. 시각은 초 단위로 늘린다."""

    def __init__(self, sid=None, t0="2026-09-30T01:00:00", cwd=CWD):
        self.sid = sid or uid()
        self.t = 0
        self.t0 = t0
        self.cwd = cwd
        self.last = None

    def ts(self, step=1):
        self.t += step
        h, rem = divmod(self.t, 3600)
        m, s = divmod(rem, 60)
        base_h = int(self.t0[11:13])
        return "%sT%02d:%02d:%02d.000Z" % (self.t0[:10], base_h + h, m, s)

    def base(self, typ, **kw):
        u = uid()
        o = {"parentUuid": self.last, "isSidechain": False, "type": typ, "uuid": u, "timestamp": self.ts(),
             "userType": "external", "entrypoint": "cli", "cwd": self.cwd, "sessionId": self.sid,
             "version": "2.1.285", "gitBranch": "HEAD"}
        o.update(kw)
        self.last = u
        return o

    def human(self, text, source="typed", content=None):
        return self.base("user", message={"role": "user", "content": content if content is not None else text},
                         promptId=uid(), origin={"kind": "human"}, promptSource=source)

    def assistant_text(self, text, mid=None, usage=None, model="claude-opus-5-5"):
        return self.base("assistant", message={"model": model, "id": mid or ("msg_" + uid().replace("-", "")[:20]),
                                               "type": "message", "role": "assistant",
                                               "content": [{"type": "text", "text": text}],
                                               "usage": usage or {"input_tokens": 3, "output_tokens": 10,
                                                                  "cache_creation_input_tokens": 100,
                                                                  "cache_read_input_tokens": 1000}},
                         requestId="req_" + uid().replace("-", "")[:20])

    def tool_use(self, name, inp, mid=None, tuid=None, usage=None):
        tuid = tuid or ("toolu_" + uid().replace("-", "")[:20])
        o = self.base("assistant", message={"model": "claude-opus-5-5", "id": mid or ("msg_" + uid().replace("-", "")[:20]),
                                            "type": "message", "role": "assistant",
                                            "content": [{"type": "tool_use", "id": tuid, "name": name, "input": inp}],
                                            "usage": usage or {"input_tokens": 1, "output_tokens": 5,
                                                               "cache_creation_input_tokens": 0,
                                                               "cache_read_input_tokens": 500}})
        return o, tuid

    def tool_result(self, tuid, text, is_error=False, tur=None, **kw):
        return self.base("user", message={"role": "user", "content": [
            {"tool_use_id": tuid, "type": "tool_result", "content": text, "is_error": is_error}]},
            toolUseResult=tur if tur is not None else {"stdout": text, "stderr": "", "interrupted": False,
                                                       "isImage": False}, **kw)

    def system(self, subtype, **kw):
        return self.base("system", subtype=subtype, **kw)

    def attachment(self, att):
        return self.base("attachment", attachment=att)

    def plain(self, typ, **kw):
        o = {"type": typ, "sessionId": self.sid}
        o.update(kw)
        return o


class CodexSess:
    """OpenAI Codex CLI rollout 한 파일의 줄을 만든다(합성, 실제 기록 아님). 줄 모양은 pwikilib/codex.py 의 설명을 따른다."""

    def __init__(self, sid=None, t0="2026-09-30T02:00:00", cwd=CWD, source="cli", ver="0.142.0"):
        self.sid = sid or uid()
        self.t = 0
        self.t0 = t0
        self.cwd = cwd
        self.source = source
        self.ver = ver

    def ts(self, step=1):
        self.t += step
        m, s = divmod(self.t, 60)
        return "%sT%s:%02d:%02d.000Z" % (self.t0[:10], self.t0[11:13], m, s)

    def path(self, env):
        d = os.path.join(env.codex, "sessions", self.t0[:4], self.t0[5:7], self.t0[8:10])
        return os.path.join(d, "rollout-%s-%s.jsonl" % (self.t0[:19].replace(":", "-"), self.sid))

    def line(self, typ, payload):
        return {"timestamp": self.ts(), "type": typ, "payload": payload}

    def meta(self):
        return self.line("session_meta", {"id": self.sid, "cwd": self.cwd, "cli_version": self.ver, "source": self.source,
                                          "originator": "codex_cli_rs", "model_provider": "openai",
                                          "git": {"branch": "main"}})

    def turn(self, model="gpt-5.5-codex"):
        return self.line("turn_context", {"cwd": self.cwd, "model": model, "effort": "high",
                                          "approval_policy": "on-request", "turn_id": uid()})

    def env_context(self):
        return self.line("response_item", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>\n  <cwd>%s</cwd>\n</environment_context>" % self.cwd}]})

    def human(self, text):
        """사람 입력 한 번 = response_item 한 줄 + event_msg 메아리 한 줄."""
        return [self.line("response_item", {"type": "message", "role": "user",
                                            "content": [{"type": "input_text", "text": text}]}),
                self.line("event_msg", {"type": "user_message", "message": text})]

    def reply(self, text):
        return [self.line("event_msg", {"type": "agent_reasoning", "text": "생각"}),
                self.line("response_item", {"type": "reasoning", "encrypted_content": "gAAAA-synthetic"}),
                self.line("response_item", {"type": "message", "role": "assistant", "id": "msg_" + uid()[:12],
                                            "content": [{"type": "output_text", "text": text}]}),
                self.line("event_msg", {"type": "agent_message", "message": text})]

    def shell(self, cmd, output, code=0):
        cid = "call_" + uid()[:12]
        return [self.line("response_item", {"type": "function_call", "name": "exec_command", "call_id": cid,
                                            "arguments": json.dumps({"cmd": cmd, "workdir": self.cwd})}),
                self.line("response_item", {"type": "function_call_output", "call_id": cid,
                                            "output": "Chunk ID: 1\nWall time: 0.1 seconds\nProcess exited with code %d\n"
                                                      "Output:\n%s" % (code, output)})]

    def patch(self, files):
        cid = "call_" + uid()[:12]
        body = "*** Begin Patch\n" + "".join("*** Update File: %s\n@@\n-a\n+b\n" % f for f in files) + "*** End Patch\n"
        return [self.line("response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": cid,
                                            "input": body}),
                self.line("response_item", {"type": "custom_tool_call_output", "call_id": cid,
                                            "output": json.dumps({"output": "Success.", "metadata": {"exit_code": 0}})})]

    def done(self, ms=4200):
        return [self.line("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10}}}),
                self.line("event_msg", {"type": "task_complete", "duration_ms": ms})]

    def basic(self, prompt="코덱스로 테스트 고쳐줘"):
        return ([self.meta(), self.turn(), self.env_context(), self.line("event_msg", {"type": "task_started"})]
                + self.human(prompt) + self.shell("pytest -q tests", "3 passed")
                + self.patch(["src/app.py", "tests/test_app.py"]) + self.reply("테스트를 고쳤습니다") + self.done())
