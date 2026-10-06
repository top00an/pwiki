"""OpenAI Codex CLI records → Claude Code-shaped lines. Used only by ingest (and discover for the session head).

Sources (nothing else under ~/.codex is ever opened: no auth.json, config.toml, sqlite state, logs, memories)
- ~/.codex/sessions/**/rollout-*.jsonl : one session per file, lines {"timestamp", "type", "payload"}
- ~/.codex/history.jsonl               : {"session_id", "ts" (epoch seconds), "text"}

normalize() turns one rollout line into either a Claude-shaped dict (stored in events.raw, so parse.classify/extract and
rederive work on it unchanged) or an exclusion reason starting with "codex:". One source line = one row or one exclusion.

Human input (deterministic, no lookahead, stable under chunked and repeated reads)
- Every real prompt is written first as response_item/message role=user, then echoed once: event_msg/user_message
  (cli <= 0.142) or event_msg/item_completed item.type=UserMessage (cli 0.149). Measured on 20 files: 336 of 336 prompts
  had exactly one echo right after, and injected messages (<environment_context>, <turn_aborted>, AGENTS.md) had none.
- So the response_item user message is the card start, unless it is injected (known tag or AGENTS.md header).
  Both echo forms are always excluded (codex:echo:user_message). No state is needed to decide.
State carried between lines (cwd, cli version, model, exec mode) comes only from session_meta and turn_context lines, which
are stored as system rows; an incremental read restores it from the last stored rows of that file (restore()).
"""
import json
import re

ENTRY = "codex"
SUB_META = "codex_session_meta"
SUB_TURN = "codex_turn_context"
FALLBACK_PROJECT = "codex-unknown-cwd"

# 사람이 친 글이 아니라 Codex 가 user 역할로 끼워 넣는 글(알려진 머리표).
INJECTED_TAGS = {"environment_context", "turn_aborted", "user_instructions", "user_shell_command", "permissions"}
_TAG_RX = re.compile(r"^\s*<([a-z_]+)[\s>]")
_IMG_TAG_RX = re.compile(r"^\s*(?:<image\b[^>]*>|</image>)\s*$")
_AGENTS_PREFIX = "# AGENTS.md instructions"
_DATA_URL_RX = re.compile(r"^data:([\w/+.-]+);base64,(.*)$", re.S)
_PATCH_FILE_RX = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)
_HEAD_RX = re.compile(r"^(?:Chunk ID|Wall time|Process exited with code|Process running with session ID|"
                      r"Original token count|Exit code|Total output lines)\b")
_CODE_RX = re.compile(r"^(?:Process exited with code|Exit code:?)\s*(-?\d+)")

# 실행 결과의 다른 사본(같은 내용이 response_item 쪽에 이미 있다)
ECHO_EVENTS = {"user_message", "agent_message", "exec_command_begin", "exec_command_end", "exec_command_output_delta",
               "patch_apply_begin", "patch_apply_end", "mcp_tool_call_begin", "mcp_tool_call_end", "web_search_begin",
               "web_search_end", "item_started", "view_image_tool_call"}
REASONING_EVENTS = {"agent_reasoning", "agent_reasoning_raw_content", "agent_reasoning_section_break"}


def new_state(sid, fid):
    return {"sid": sid, "fid": fid, "cwd": None, "ver": None, "model": None, "auto": False}


def restore(st, raw_obj):
    """저장된 codex system 줄(가린 뒤 raw)에서 상태를 되살린다(이어 읽기용)."""
    if not isinstance(raw_obj, dict):
        return
    if isinstance(raw_obj.get("cwd"), str):
        st["cwd"] = raw_obj["cwd"]
    if isinstance(raw_obj.get("version"), str):
        st["ver"] = raw_obj["version"]
    cx = raw_obj.get("codex") if isinstance(raw_obj.get("codex"), dict) else {}
    if raw_obj.get("subtype") == SUB_META:
        st["auto"] = bool(cx.get("auto"))
    if isinstance(cx.get("model"), str):
        st["model"] = cx["model"]


def session_head(o):
    """discover 용: 첫 줄(session_meta) 에서 (세션 id, cwd)."""
    if isinstance(o, dict) and o.get("type") == "session_meta" and isinstance(o.get("payload"), dict):
        p = o["payload"]
        sid = p.get("id") if isinstance(p.get("id"), str) else None
        cwd = p.get("cwd") if isinstance(p.get("cwd"), str) else None
        return sid, cwd
    if isinstance(o, dict) and o.get("type") == "turn_context" and isinstance(o.get("payload"), dict):
        cwd = o["payload"].get("cwd")
        return None, cwd if isinstance(cwd, str) else None
    return None, None


def _base(st, o, typ):
    d = {"type": typ, "sessionId": st["sid"], "timestamp": o.get("timestamp"), "entrypoint": ENTRY}
    if st["cwd"]:
        d["cwd"] = st["cwd"]
    if st["ver"]:
        d["version"] = st["ver"]
    return d


def _cmd_str(v):
    if isinstance(v, str):
        return v
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        if len(v) == 3 and v[1] in ("-lc", "-c") and v[0].rsplit("/", 1)[-1] in ("bash", "sh", "zsh"):
            return v[2]
        return " ".join(v)
    return None


def _image_block(url):
    m = _DATA_URL_RX.match(url) if isinstance(url, str) else None
    if m:
        return {"type": "image", "source": {"type": "base64", "media_type": m.group(1), "data": m.group(2)}}
    return {"type": "text", "text": "[이미지]"}


def _user_blocks(content):
    out = []
    for c in content if isinstance(content, list) else []:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t in ("input_text", "text"):
            txt = c.get("text") or ""
            if _IMG_TAG_RX.match(txt):
                continue
            out.append({"type": "text", "text": txt})
        elif t == "input_image":
            out.append(_image_block(c.get("image_url")))
    return out


def _injected(content):
    """끼워 넣은 user 글이면 사유, 사람 입력이면 None."""
    for c in content if isinstance(content, list) else []:
        if not isinstance(c, dict) or c.get("type") not in ("input_text", "text"):
            continue
        txt = c.get("text") or ""
        if _IMG_TAG_RX.match(txt):
            continue  # 이미지 감싸개 줄은 판정 근거가 아니다(뒤의 글을 본다)
        if txt.startswith(_AGENTS_PREFIX):
            return "codex:injected:agents_md"
        m = _TAG_RX.match(txt)
        if m and m.group(1) in INJECTED_TAGS:
            return "codex:injected:" + m.group(1)
        return None
    return None


def _tid(v, st, off):
    """도구 id. 원천에 없으면 파일·오프셋으로 만든다(tool_uses.tuid 는 전역 키라 'None' 이 겹치면 안 된다)."""
    return v if isinstance(v, str) and v else "cx:%s:%d" % (st["fid"], off)


def _tool_name(p):
    name = p.get("name") if isinstance(p.get("name"), str) else "?"
    ns = p.get("namespace")
    if isinstance(ns, str) and ns:
        return ns.rstrip("_") + "__" + name
    return name


def _args(p):
    a = p.get("arguments")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except ValueError:
            return {"_raw": a}
    return a if isinstance(a, dict) else {"_raw": a}


def _shell_input(name, args):
    d = dict(args)
    for k in ("cmd", "command"):
        if k in d:
            s = _cmd_str(d.pop(k))
            if s is not None:
                d["command"] = s
            break
    return d


def _patch_blocks(call_id, name, patch, files=None):
    """apply_patch 한 번을 바뀐 파일마다 tool_use 하나로(첫 블록만 패치 글을 싣는다). 파일 경로는 fpath 로 뽑힌다."""
    if files is None:
        files = []
        for m in _PATCH_FILE_RX.finditer(patch or ""):
            f = m.group(1) or m.group(2)
            if f and f not in files:
                files.append(f)
    blocks = []
    for i, f in enumerate(files or [None]):
        inp = {}
        if f:
            inp["file_path"] = f
        if i == 0 and patch:
            inp["content"] = patch
        blocks.append({"type": "tool_use", "id": call_id if i == 0 else "%s#%d" % (call_id, i + 1), "name": name,
                       "input": inp})
    return blocks


def _assistant(st, o, blocks, mid=None):
    d = _base(st, o, "assistant")
    m = {"role": "assistant", "type": "message", "content": blocks}
    if mid:
        m["id"] = mid
        if st["model"]:
            m["model"] = st["model"]
    d["message"] = m
    return d


def _split_output(text):
    """실행 결과 머리(Chunk ID·Wall time·종료 코드·Output:)를 떼고 (종료 코드, 본문)."""
    lines = text.split("\n")
    code = None
    for i, ln in enumerate(lines[:10]):
        if ln.startswith("Output:"):
            body = "\n".join([ln[len("Output:"):].lstrip()] + lines[i + 1:]).lstrip("\n")
            return code, body
        m = _CODE_RX.match(ln)
        if m:
            code = int(m.group(1))
        elif not _HEAD_RX.match(ln):
            break
    return None, text


def _result(st, o, call_id, out):
    """함수·사용자 정의 도구 결과 → tool_result. 종료 코드가 0 이 아니면 is_error, 글 머리는 Claude 의 'Exit code N'."""
    is_err = False
    content = None
    if isinstance(out, str):
        code, body = None, out
        try:
            j = json.loads(out) if out[:1] == "{" else None
        except ValueError:
            j = None
        if isinstance(j, dict) and "output" in j:
            meta = j.get("metadata") if isinstance(j.get("metadata"), dict) else {}
            code = meta.get("exit_code") if isinstance(meta.get("exit_code"), int) else None
            body = j.get("output") if isinstance(j.get("output"), str) else json.dumps(j.get("output"), ensure_ascii=False)
        else:
            code, body = _split_output(out)
        if code not in (None, 0):
            is_err = True
            body = "Exit code %d\n%s" % (code, body)
        content = body
    elif isinstance(out, list):
        content = []
        for c in out:
            if not isinstance(c, dict):
                continue
            if c.get("type") in ("input_text", "output_text", "text"):
                content.append({"type": "text", "text": c.get("text") or ""})
            elif c.get("type") in ("input_image", "output_image"):
                content.append(_image_block(c.get("image_url")))
        first = content[0].get("text", "") if content and content[0].get("type") == "text" else ""
        is_err = first.startswith("Script failed")
    else:
        content = json.dumps(out, ensure_ascii=False)
    d = _base(st, o, "user")
    d["message"] = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": content,
                                                 "is_error": is_err}]}
    return d


def _system(st, o, sub, **kw):
    d = _base(st, o, "system")
    d["subtype"] = sub
    d.update(kw)
    return d


def normalize(o, st, off):
    """(Claude 모양 dict, None) 또는 (None, 'codex:…' 제외 사유). st 는 세션 상태(제자리에서 바뀐다)."""
    t = o.get("type")
    p = o.get("payload") if isinstance(o.get("payload"), dict) else {}
    pt = p.get("type")
    if t == "session_meta":
        if isinstance(p.get("cwd"), str):
            st["cwd"] = p["cwd"]
        if isinstance(p.get("cli_version"), str):
            st["ver"] = p["cli_version"]
        st["auto"] = p.get("source") == "exec" or p.get("originator") == "codex_exec"
        git = p.get("git") if isinstance(p.get("git"), dict) else {}
        cx = {"originator": p.get("originator"), "source": p.get("source") if isinstance(p.get("source"), str) else None,
              "model_provider": p.get("model_provider"), "auto": st["auto"]}
        d = _system(st, o, SUB_META, content="Codex 세션 시작 (%s %s)" % (p.get("originator") or "codex",
                                                                    p.get("cli_version") or "?"), codex=cx)
        if isinstance(git.get("branch"), str):
            d["gitBranch"] = git["branch"]
        return d, None
    if t == "turn_context":
        if isinstance(p.get("cwd"), str):
            st["cwd"] = p["cwd"]
        if isinstance(p.get("model"), str):
            st["model"] = p["model"]
        cx = {k: p.get(k) for k in ("model", "effort", "approval_policy", "turn_id") if isinstance(p.get(k), str)}
        return _system(st, o, SUB_TURN, content=" · ".join("%s %s" % kv for kv in cx.items() if kv[0] != "turn_id"),
                       codex=cx), None
    if t == "compacted":
        msg = p.get("message") if isinstance(p.get("message"), str) else ""
        d = _base(st, o, "user")
        d["isCompactSummary"] = True
        d["message"] = {"role": "user", "content": [{"type": "text", "text": msg}]}
        return d, None
    if t == "response_item":
        if pt == "message":
            role = p.get("role")
            if role == "user":
                why = _injected(p.get("content"))
                if why:
                    return None, why
                d = _base(st, o, "user")
                d["promptSource"] = "sdk" if st["auto"] else "typed"
                d["message"] = {"role": "user", "content": _user_blocks(p.get("content"))}
                return d, None
            if role == "assistant":
                blocks = []
                for c in p.get("content") if isinstance(p.get("content"), list) else []:
                    if isinstance(c, dict) and c.get("type") in ("output_text", "text", "refusal"):
                        blocks.append({"type": "text", "text": c.get("text") or c.get("refusal") or ""})
                mid = p.get("id") if isinstance(p.get("id"), str) and p.get("id") else "cx:%s:%d" % (st["fid"], off)
                return _assistant(st, o, blocks, mid), None
            return None, "codex:%s" % ("developer" if role in ("developer", "system") else "message:%s" % role)
        if pt == "reasoning":
            return None, "codex:reasoning"
        if pt == "function_call":
            name = _tool_name(p)
            args = _args(p)
            if name in ("exec_command", "shell", "shell_command", "local_shell"):
                args = _shell_input(name, args)
            return _assistant(st, o, [{"type": "tool_use", "id": _tid(p.get("call_id"), st, off), "name": name,
                                       "input": args}]), None
        if pt == "local_shell_call":
            act = p.get("action") if isinstance(p.get("action"), dict) else {}
            inp = {k: v for k, v in act.items() if k not in ("type", "command")}
            cs = _cmd_str(act.get("command"))
            if cs is not None:
                inp["command"] = cs
            return _assistant(st, o, [{"type": "tool_use", "id": _tid(p.get("call_id") or p.get("id"), st, off),
                                       "name": "local_shell",
                                       "input": inp}]), None
        if pt == "custom_tool_call":
            name = _tool_name(p)
            inp = p.get("input")
            if name == "apply_patch":
                return _assistant(st, o, _patch_blocks(_tid(p.get("call_id"), st, off), name,
                                                       inp if isinstance(inp, str) else "")), None
            return _assistant(st, o, [{"type": "tool_use", "id": _tid(p.get("call_id"), st, off), "name": name,
                                       "input": {"content": inp} if isinstance(inp, str) else {"_raw": inp}}]), None
        if pt in ("function_call_output", "custom_tool_call_output", "local_shell_call_output"):
            return _result(st, o, p.get("call_id"), p.get("output")), None
        if pt == "web_search_call":
            act = p.get("action") if isinstance(p.get("action"), dict) else {}
            tid = _tid(p.get("id"), st, off)
            return _assistant(st, o, [{"type": "tool_use", "id": tid, "name": "web_search",
                                       "input": {k: v for k, v in act.items() if k != "type"}}]), None
        if pt in ("tool_search_call", "tool_search_output"):
            return None, "codex:tool_search"
        return None, "codex:response_item:%s" % pt
    if t == "event_msg":
        if pt in ECHO_EVENTS:
            return None, "codex:echo:%s" % pt
        if pt in REASONING_EVENTS:
            return None, "codex:reasoning"
        if pt == "token_count":
            return None, "codex:token_count"
        if pt == "task_started":
            return None, "codex:task_started"
        if pt == "task_complete":
            ms = p.get("duration_ms")
            return _system(st, o, "turn_duration", durationMs=int(ms) if isinstance(ms, (int, float)) else 0), None
        if pt == "turn_aborted":
            d = _base(st, o, "user")
            d["message"] = {"role": "user", "content": [
                {"type": "text", "text": "[Request interrupted by user] codex: %s" % (p.get("reason") or "aborted")}]}
            return d, None
        if pt == "context_compacted":
            return _system(st, o, "compact_boundary", content="Conversation compacted"), None
        if pt == "item_completed":
            it = p.get("item") if isinstance(p.get("item"), dict) else {}
            itt = it.get("type")
            if itt == "UserMessage":
                return None, "codex:echo:user_message"
            if itt == "AgentMessage":
                return None, "codex:echo:agent_message"
            if itt == "Reasoning":
                return None, "codex:reasoning"
            if itt == "CommandExecution":
                inp = {"command": _cmd_str(it.get("command"))}
                if isinstance(it.get("cwd"), str):
                    inp["workdir"] = it["cwd"]
                return _assistant(st, o, [{"type": "tool_use", "id": _tid(it.get("id"), st, off), "name": "exec_command",
                                           "input": inp}]), None
            if itt == "FileChange":
                ch = it.get("changes")
                files = sorted(ch) if isinstance(ch, dict) else [c.get("path") for c in ch or []
                                                                  if isinstance(c, dict) and isinstance(c.get("path"), str)]
                return _assistant(st, o, _patch_blocks(_tid(it.get("id"), st, off), "file_change", None, files)), None
            return None, "codex:item:%s" % itt
        if pt == "thread_settings_applied":
            return None, "codex:thread_settings"
        return None, "codex:event:%s" % pt
    if t == "world_state":
        return None, "codex:world_state"
    return None, "codex:type:%s" % t


def normalize_history(o):
    """~/.codex/history.jsonl 한 줄 → Claude history 모양."""
    if not isinstance(o.get("text"), str):
        return None, "codex:history_bad"
    d = {"type": "history", "display": o["text"], "timestamp": o.get("ts")}
    if isinstance(o.get("session_id"), str):
        d["sessionId"] = o["session_id"]
    return d, None
