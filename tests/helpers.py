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
        os.makedirs(os.path.join(self.claude, "projects", PROJ), exist_ok=True)
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        self.old = {k: os.environ.get(k) for k in ("PWIKI_CLAUDE_DIR", "PWIKI_HOME", "PWIKI_VAULT")}
        os.environ["PWIKI_CLAUDE_DIR"] = self.claude
        os.environ["PWIKI_HOME"] = self.home
        os.environ["PWIKI_VAULT"] = self.vault
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
