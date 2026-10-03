#!/usr/bin/python3
"""합성 Claude Code 기록 생성기(시험·시연용). 실제 기록은 읽지도 복사하지도 않는다.

만드는 것(--claude-dir 아래):
  projects/<프로젝트>/<세션>.jsonl      사람 입력·답·도구 호출과 결과·압축 요약·/clear·하위 에이전트·워크플로 호출
  projects/<프로젝트>/<세션>/subagents/   하위 에이전트 기록과 meta, 워크플로 저널·에이전트 기록
  projects/<프로젝트>/<세션>/workflows/   워크플로 실행 기록(wf_*.json)
  projects/<프로젝트>/memory/*.md        메모리 문서
  history.jsonl                         사람 프롬프트 기록
가짜 비밀값(비밀번호 문맥·토큰 모양)을 몇 개 넣는다. 값은 씨앗으로 만든 난수라 소스에 리터럴로 없다.
--secrets-out 을 주면 가짜 값 목록을 JSON 으로 쓴다(시험이 가림을 확인하는 데 쓴다).

사용: make_fixture.py --claude-dir DIR [--date YYYY-MM-DD(KST, 기본 오늘)] [--cwd-root DIR] [--seed N]
      [--secrets-out PATH]
"""
import argparse
import datetime
import json
import os
import random
import sys
import uuid

KST = datetime.timezone(datetime.timedelta(hours=9))
MODEL = "claude-opus-5-5"
VERSION = "2.1.285"


class Rand:
    def __init__(self, seed):
        self.r = random.Random(seed)

    def uuid(self):
        return str(uuid.UUID(int=self.r.getrandbits(128), version=4))

    def chars(self, n, alphabet):
        return "".join(self.r.choice(alphabet) for _ in range(n))

    def mixed(self, n):
        """글자·숫자가 섞인 값(앞 두 자리는 대문자·숫자로 고정해 강한 값이 되게 한다)."""
        al = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return self.chars(1, "ABCDEFGHJKLMNPQRSTUVWXYZ") + self.chars(1, "23456789") + self.chars(n - 2, al)


def fake_secrets(rnd):
    up = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    return {
        "db_password": rnd.mixed(12) + "#" + rnd.mixed(3),     # PASSWORD= 문맥
        "mysql_password": rnd.mixed(11) + "!",                  # mysql -p 문맥
        "github_token": "gh" + "p_" + rnd.mixed(36),            # 토큰 모양
        "aws_key": "AK" + "IA" + rnd.chars(16, up),             # 토큰 모양
        "bearer": rnd.mixed(40),                                 # Authorization: Bearer 문맥
        "known": rnd.mixed(10) + "#" + rnd.mixed(5),            # 문맥 없음: secrets.local 에 적어야 가려진다
    }


class Sess:
    def __init__(self, rnd, cwd, t0, sid=None):
        self.rnd = rnd
        self.sid = sid or rnd.uuid()
        self.cwd = cwd
        self.t = t0
        self.last = None

    def ts(self, step=7):
        self.t += datetime.timedelta(seconds=step)
        return self.t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
            "%03dZ" % (self.t.microsecond // 1000)

    def base(self, typ, **kw):
        u = self.rnd.uuid()
        o = {"parentUuid": self.last, "isSidechain": False, "type": typ, "uuid": u, "timestamp": self.ts(),
             "userType": "external", "entrypoint": "cli", "cwd": self.cwd, "sessionId": self.sid,
             "version": VERSION, "gitBranch": "main"}
        o.update(kw)
        self.last = u
        return o

    def human(self, text):
        return self.base("user", message={"role": "user", "content": text}, promptId=self.rnd.uuid(),
                         origin={"kind": "human"}, promptSource="typed")

    def command(self, name, args=""):
        return self.base("user", message={"role": "user", "content": "<command-name>%s</command-name>\n"
                                          "<command-message>%s</command-message>\n<command-args>%s</command-args>"
                                          % (name, name.lstrip("/"), args)})

    def _mid(self):
        return "msg_" + self.rnd.chars(20, "0123456789abcdef")

    def say(self, text, out_tokens=40):
        return self.base("assistant", message={"model": MODEL, "id": self._mid(), "type": "message",
                                               "role": "assistant", "content": [{"type": "text", "text": text}],
                                               "usage": {"input_tokens": 5, "output_tokens": out_tokens,
                                                         "cache_creation_input_tokens": 200,
                                                         "cache_read_input_tokens": 3000}},
                         requestId="req_" + self.rnd.chars(20, "0123456789abcdef"))

    def tool(self, name, inp):
        tuid = "toolu_" + self.rnd.chars(20, "0123456789abcdef")
        o = self.base("assistant", message={"model": MODEL, "id": self._mid(), "type": "message", "role": "assistant",
                                            "content": [{"type": "tool_use", "id": tuid, "name": name, "input": inp}],
                                            "usage": {"input_tokens": 2, "output_tokens": 15,
                                                      "cache_creation_input_tokens": 0,
                                                      "cache_read_input_tokens": 3200}})
        return o, tuid

    def result(self, tuid, text, is_error=False, tur=None):
        return self.base("user", message={"role": "user", "content": [
            {"tool_use_id": tuid, "type": "tool_result", "content": text, "is_error": is_error}]},
            toolUseResult=tur if tur is not None else {"stdout": text, "stderr": "", "interrupted": False,
                                                       "isImage": False})

    def compact(self, summary):
        return [self.base("system", subtype="compact_boundary", content="Conversation compacted",
                          compactMetadata={"trigger": "manual", "preTokens": 52000}),
                self.base("user", message={"role": "user", "content": summary}, isCompactSummary=True,
                          isVisibleInTranscriptOnly=True)]

    def todo(self, items):
        o, t = self.tool("TodoWrite", {"todos": [{"content": c, "status": s, "activeForm": c} for c, s in items]})
        return [o, self.result(t, "Todos have been modified successfully.")]


def write_jsonl(path, objs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for o in objs:
            fh.write(json.dumps(o, ensure_ascii=False) + "\n")


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def proj_dir_name(cwd):
    return cwd.replace("/", "-").replace(".", "-")


def build(claude_dir, date_kst, cwd_root, seed=1004):
    rnd = Rand(seed)
    sec = fake_secrets(rnd)
    day = datetime.datetime.strptime(date_kst, "%Y-%m-%d").replace(tzinfo=KST)
    alpha_cwd = os.path.join(cwd_root, "work", "alpha-api")
    beta_cwd = os.path.join(cwd_root, "work", "beta-tools")
    projects = os.path.join(claude_dir, "projects")
    history = []
    info = {"projects": {}, "sessions": [], "secrets": sec, "date": date_kst}

    def hist(sess, text):
        history.append({"display": text, "pastedContents": {}, "timestamp": int(sess.t.timestamp() * 1000) + 500,
                        "project": sess.cwd, "sessionId": sess.sid})

    # ---- 프로젝트 alpha: 세션 1(작업·압축·/clear) -------------------------------------------
    a_dir = os.path.join(projects, proj_dir_name(alpha_cwd))
    s1 = Sess(rnd, alpha_cwd, day.replace(hour=9, minute=5))
    L = []
    q1 = "로그인 API 에 비밀번호 재설정 엔드포인트를 붙여줘"
    L.append(s1.human(q1)); hist(s1, q1)
    L.append(s1.say("재설정 토큰을 메일로 보내는 흐름으로 만들겠습니다. 먼저 구조를 봅니다."))
    o, t = s1.tool("Bash", {"command": "ls src/auth", "description": "인증 폴더 보기"})
    L += [o, s1.result(t, "login.py\nsession.py\n")]
    o, t = s1.tool("Bash", {"command": "mysql -u app -p'%s' -h 10.0.0.5 -e 'show tables' authdb" % sec["mysql_password"],
                            "description": "표 목록"})
    L += [o, s1.result(t, "users\nreset_tokens\n")]
    o, t = s1.tool("Write", {"file_path": os.path.join(alpha_cwd, ".env.local"),
                             "content": "DB_PASSWORD=%s\nAPP_ENV=dev\n" % sec["db_password"]})
    L += [o, s1.result(t, "File created successfully")]
    o, t = s1.tool("Bash", {"command": "pytest -q tests/test_reset.py"})
    L += [o, s1.result(t, "Exit code 1\nFAILED tests/test_reset.py::test_expired - AssertionError: 410 != 400",
                       is_error=True)]
    o, t = s1.tool("Edit", {"file_path": os.path.join(alpha_cwd, "src/auth/reset.py"),
                            "old_string": "return 400", "new_string": "return 410"})
    L += [o, s1.result(t, "The file has been updated.")]
    L += s1.todo([("재설정 엔드포인트", "completed"), ("만료 토큰 시험", "completed"), ("메일 템플릿 다듬기", "pending")])
    L.append(s1.say("재설정 엔드포인트를 붙였고 만료 토큰은 410 을 돌려줍니다. 메일 템플릿은 아직입니다."))
    q2 = "메일 발송은 큐로 빼자. 하위 에이전트로 검토도 받아줘"
    L.append(s1.human(q2)); hist(s1, q2)
    agent_id = rnd.chars(16, "0123456789abcdef")
    o, t = s1.tool("Agent", {"description": "재설정 흐름 검토", "prompt": "검토해", "subagent_type": "general-purpose"})
    L += [o, s1.result(t, "Async agent launched successfully.\nagentId: %s" % agent_id,
                       tur={"agentId": agent_id, "status": "async_launched"})]
    L.append(s1.say("검토 결과: 토큰 재사용 방지가 필요합니다. 반영했습니다."))
    L += s1.compact("This session is being continued from a previous conversation.\nSummary:\n"
                    "## 1. Primary Request and Intent\n비밀번호 재설정 엔드포인트와 메일 큐\n"
                    "## 2. Key Technical Concepts\n- 재설정 토큰, 만료 410\n"
                    "## 3. Pending Tasks\n- 메일 템플릿 다듬기\n")
    q3 = "이어서 메일 템플릿 다듬어"
    L.append(s1.human(q3)); hist(s1, q3)
    L.append(s1.say("템플릿 제목과 본문을 정리했습니다."))
    L.append(s1.command("/clear")); hist(s1, "/clear")
    write_jsonl(os.path.join(a_dir, s1.sid + ".jsonl"), L)
    sub = Sess(rnd, alpha_cwd, s1.t, sid=s1.sid)
    a1 = sub.base("user", message={"role": "user", "content": "재설정 흐름을 검토해"}, isSidechain=True,
                  agentId=agent_id)
    a2 = sub.say("토큰을 한 번 쓰면 지우세요.", out_tokens=90)
    a2["isSidechain"] = True
    a2["agentId"] = agent_id
    sd = os.path.join(a_dir, s1.sid, "subagents")
    write_jsonl(os.path.join(sd, "agent-%s.jsonl" % agent_id), [a1, a2])
    write_text(os.path.join(sd, "agent-%s.meta.json" % agent_id),
               json.dumps({"agentType": "general-purpose", "description": "재설정 흐름 검토", "toolUseId": t}))
    info["sessions"].append(s1.sid)

    # ---- 프로젝트 alpha: 세션 2(/clear 뒤 새 세션, 워크플로) ----------------------------------------
    s2 = Sess(rnd, alpha_cwd, s1.t + datetime.timedelta(minutes=3))
    L = []
    q4 = "배포 전에 워크플로로 시험 세 갈래 돌려줘"
    L.append(s2.human(q4)); hist(s2, q4)
    run_id = "wf_" + rnd.chars(8, "0123456789abcdef") + "-001"
    o, t = s2.tool("Workflow", {"script": "export const workflow = {name: '배포 전 시험'}"})
    L += [o, s2.result(t, "Workflow launched. runId: %s" % run_id, tur={"runId": run_id})]
    o, t = s2.tool("Bash", {"command": "curl -H 'Authorization: Bearer %s' https://api.example.com/v1/ping"
                                       % sec["bearer"]})
    L += [o, s2.result(t, '{"ok": true}')]
    L.append(s2.say("세 갈래 모두 통과했습니다. 배포 준비가 됐습니다."))
    write_jsonl(os.path.join(a_dir, s2.sid + ".jsonl"), L)
    wd = os.path.join(a_dir, s2.sid, "subagents", "workflows", run_id)
    write_jsonl(os.path.join(wd, "journal.jsonl"), [{"type": "launched"},
                                                    {"type": "result", "key": "unit", "result": {"verdict": "통과"}}])
    w = Sess(rnd, alpha_cwd, s2.t, sid=s2.sid)
    write_jsonl(os.path.join(wd, "agent-%s.jsonl" % rnd.chars(12, "0123456789abcdef")),
                [w.say("단위 시험 통과", out_tokens=120)])
    write_text(os.path.join(a_dir, s2.sid, "workflows", run_id + ".json"),
               json.dumps({"runId": run_id, "workflowName": "배포 전 시험", "result": {"passed": 3}}, ensure_ascii=False))
    info["sessions"].append(s2.sid)

    write_text(os.path.join(a_dir, "memory", "MEMORY.md"),
               "# Memory Index\n\n- [재설정 흐름](reset-flow.md) — 만료 토큰은 410\n")
    write_text(os.path.join(a_dir, "memory", "reset-flow.md"),
               "---\nname: reset-flow\n---\n재설정 토큰은 한 번 쓰면 지운다. 만료는 410.\n")
    info["projects"]["alpha"] = {"dir": proj_dir_name(alpha_cwd), "cwd": alpha_cwd}

    # ---- 프로젝트 beta: 세션 3(데이터 정리, 토큰 모양 값) --------------------------------------------
    b_dir = os.path.join(projects, proj_dir_name(beta_cwd))
    s3 = Sess(rnd, beta_cwd, day.replace(hour=14, minute=20))
    L = []
    q5 = "CSV 정리 스크립트에서 중복 행을 빼줘"
    L.append(s3.human(q5)); hist(s3, q5)
    o, t = s3.tool("Bash", {"command": "python3 dedupe.py data/orders.csv"})
    L += [o, s3.result(t, "rows 1200 -> 1187")]
    o, t = s3.tool("Bash", {"command": "git push https://%s@git.example.com/acme/beta-tools.git" % sec["github_token"]})
    L += [o, s3.result(t, "Everything up-to-date")]
    o, t = s3.tool("Bash", {"command": "aws s3 ls", "description": "버킷 보기"})
    L += [o, s3.result(t, "aws_access_key_id = %s\n2026-01-01 backups" % sec["aws_key"])]
    o, t = s3.tool("Bash", {"command": "cat notes.txt"})
    L += [o, s3.result(t, "메모: 운영 값은 %s 로 바꿨다" % sec["known"])]
    L.append(s3.say("중복 13행을 뺐습니다. 기준 열은 order_id 입니다."))
    write_jsonl(os.path.join(b_dir, s3.sid + ".jsonl"), L)
    write_text(os.path.join(b_dir, "memory", "MEMORY.md"), "# Memory Index\n\n- 중복 기준 열은 order_id\n")
    info["sessions"].append(s3.sid)
    info["projects"]["beta"] = {"dir": proj_dir_name(beta_cwd), "cwd": beta_cwd}

    write_jsonl(os.path.join(claude_dir, "history.jsonl"), history)
    info["prompts"] = [q1, q2, q3, q4, q5]
    return info


def main(argv=None):
    ap = argparse.ArgumentParser(description="합성 Claude Code 기록을 만든다(실제 기록 없음)")
    ap.add_argument("--claude-dir", required=True)
    ap.add_argument("--date", default=datetime.datetime.now(tz=KST).strftime("%Y-%m-%d"), help="KST 날짜")
    ap.add_argument("--cwd-root", default="/home/tester", help="가짜 작업 폴더의 뿌리")
    ap.add_argument("--seed", type=int, default=1004)
    ap.add_argument("--secrets-out", help="가짜 값 목록과 프로젝트 정보를 쓸 JSON 경로(권한 600)")
    a = ap.parse_args(argv)
    if os.path.isdir(os.path.join(a.claude_dir, "projects")) and os.listdir(os.path.join(a.claude_dir, "projects")):
        sys.stderr.write("멈춤: %s/projects 가 비어 있지 않다(실제 기록 위에 만들지 않는다)\n" % a.claude_dir)
        return 2
    info = build(a.claude_dir, a.date, a.cwd_root, a.seed)
    if a.secrets_out:
        fd = os.open(a.secrets_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(info, fh, ensure_ascii=False, indent=1)
    print("합성 기록: 프로젝트 %d · 세션 %d · 프롬프트 %d · 가짜 비밀값 %d" % (
        len(info["projects"]), len(info["sessions"]), len(info["prompts"]), len(info["secrets"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
