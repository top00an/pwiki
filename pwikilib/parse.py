"""Classify, clean and extract one record line. This is the only parser.

Order: json.loads → clean (large non-knowledge fields to placeholders) → redact (all strings) → extract (body, tools, errors, tokens).
Cleaning only deletes values, so running it before redaction leaks nothing. Truncation (FTS cap) happens only after redaction.
"""
import hashlib
import json
import re

# ---- 줄 종류 정책 -------------------------------------------------------------
# 통째로 빼는 줄: 사유별로 센다. 모르는 type 은 unknown_type:<t> 로 센다.
EXCLUDE_TYPES = {
    "last-prompt": "state:last-prompt",
    "atis-latch": "state:atis-latch",
    "bridge-session": "state:bridge-session",
    "file-history-snapshot": "file_backup",
    "file-history-delta": "file_backup",
    "artifact-autoreact-ledger": "state:artifact",
    "artifact-comment-monitor": "state:artifact",
}
KEEP_TYPES = {
    "user", "assistant", "system", "attachment", "queue-operation", "ai-title", "custom-title",
    "permission-mode", "mode", "agent-setting", "frame-link", "cost-state", "summary",
    "launched", "started", "result", "fork-context-ref", "history",
}
EXCLUDE_SYSTEM = {"stop_hook_summary": "hook_summary"}
KNOWN_SYSTEM = {
    "turn_duration", "away_summary", "local_command", "compact_boundary", "informational",
    "bridge_status", "scheduled_task_fire", "model_refusal_fallback", "api_error",
}
EXCLUDE_ATT = {
    "hook_success": "hook_success",
    "hook_additional_context": "noise:hook_additional_context",
    "total_tokens_reminder": "noise:total_tokens_reminder",
    "output_style": "noise:output_style",
    "output_style_instructions": "noise:output_style_instructions",
    "batching_reminder_sent": "noise:batching_reminder_sent",
    "bash_output_audience_note": "noise:bash_output_audience_note",
    "silent_turn_reminder": "noise:silent_turn_reminder",
    "deferred_tools_delta": "noise:deferred_tools",
    "deferred_tools_record": "noise:deferred_tools",
    "mcp_instructions_delta": "noise:mcp_instructions",
    "agent_listing_delta": "noise:agent_listing",
    "skill_listing": "noise:skill_listing",
    "prompt_snapshot": "noise:prompt_snapshot",
    "instructions": "noise:instructions",
    "auto_mode": "noise:auto_mode",
    "session_context": "noise:session_context",
    "credential_org": "noise:credential_org",
    "language": "noise:language",
    "thinking_drop": "noise:thinking_drop",
    "thinking_stripped": "noise:thinking_stripped",
    "remote_session_change": "noise:remote_session_change",
    "command_permissions": "noise:command_permissions",
}
KEEP_ATT = {
    "queued_command", "edited_text_file", "file", "compact_file_reference", "inlined_image_paths",
    "goal_status", "invoked_skills", "date", "date_change", "environment", "model",
    "hook_permission_decision", "hook_cancelled", "read_truncation_notice", "structured_output",
    "task_reminder", "ultra_effort_enter",
}

# 조사에서 본 최상위 필드. 여기에 없는 필드는 raw 에는 남기되 해석하지 않고 unknown_field:<type>.<필드> 로 센다
# (필드가 옮겨지거나 새로 생기는 형식 변화를 개수로 알아채기 위함).
_COMMON = {"parentUuid", "isSidechain", "type", "uuid", "timestamp", "userType", "entrypoint", "cwd", "sessionId",
           "version", "gitBranch", "session_id", "slug", "teamName", "agentName", "agentId"}
KNOWN_FIELDS = {
    "user": _COMMON | {"message", "promptId", "toolUseResult", "sourceToolAssistantUUID", "origin", "promptSource",
                       "permissionMode", "serverClassifierContext", "isMeta", "turnOrigin", "turnCompanion",
                       "queueSkipAttachments", "interruptedMessageId", "toolDenialKind", "turnPosition", "sourceToolUseID",
                       "imagePasteIds", "isVisibleInTranscriptOnly", "isCompactSummary", "queueOrigin", "queuePriority",
                       "mcpMeta", "userFeedback", "scheduledTaskId", "scheduledFireId"},
    "assistant": _COMMON | {"message", "requestId", "effort", "apiBlockIndex", "perTurnEffort", "advisorModel",
                            "serverClassifierRequest", "wireToolInputs", "wireIngestContext", "attributionSkill",
                            "attributionMcpServer", "attributionMcpTool", "attributionPlugin", "attributionAgent", "error",
                            "isApiErrorMessage", "isAbortedMidStream", "apiErrorStatus", "quotaLimits",
                            "truncatedAfterOutput", "errorDetails"},
    "system": _COMMON | {"subtype", "isMeta", "level", "hookCount", "hookInfos", "hookErrors", "hookAdditionalContext",
                         "preventedContinuation", "stopReason", "hasOutput", "toolUseID", "durationMs", "messageCount",
                         "content", "pendingWorkflowCount", "pendingBackgroundAgentCount", "logicalParentUuid",
                         "compactMetadata", "url", "commandRun", "taskId", "cron", "prompt", "trigger", "direction", "scope",
                         "originalModel", "fallbackModel", "requestId", "apiRefusalCategory", "apiRefusalExplanation",
                         "retractedMessageUuids", "refusedUserMessageUuid"},
    "attachment": _COMMON | {"attachment", "rendered", "renderedInHumanTurn", "renderedRole"},
    "queue-operation": {"type", "operation", "timestamp", "sessionId", "content", "reason", "commandUuid"},
    "ai-title": {"type", "aiTitle", "sessionId"},
    "permission-mode": {"type", "permissionMode", "sessionId"},
    "mode": {"type", "mode", "sessionId"},
    "agent-setting": {"type", "agentSetting", "sessionId"},
    "frame-link": {"type", "sessionId", "artifactCount", "timestamp", "path", "frameUrl", "title"},
    "cost-state": {"type", "sessionId", "totalCostUSD", "totalAPIDuration", "totalAPIDurationWithoutRetries",
                   "totalToolDuration", "totalLinesAdded", "totalLinesRemoved", "totalDuration", "startTime", "modelUsage",
                   "hasUnknownModelCost"},
    "history": {"type", "display", "pastedContents", "timestamp", "project", "sessionId"},
}


def unknown_fields(o):
    t = o.get("type")
    known = KNOWN_FIELDS.get(t)
    if known is None:
        return []
    return ["%s.%s" % (t, k) for k in o if k not in known]


CHANGE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SUBAGENT_TOOLS = {"Agent", "Task"}
WORKFLOW_TOOLS = {"Workflow"}
TODO_TOOLS = {"TodoWrite"}
PLAN_TOOLS = {"EnterPlanMode", "ExitPlanMode"}

DENIAL_MARKERS = (
    "The user doesn't want to proceed with this tool use",
    "Permission for this action was denied",
    "Permission to use",
    "User rejected",
)
INTERRUPT_MARK = "[Request interrupted by user"


def line_policy(o):
    """(유지 여부, 사유). 제외면 사유는 제외 사유다. 모르는 type·첨부는 유지하되 사유에 'unknown_type:<t>' 나
    'unknown_attachment:<t>' 를 돌려준다(가린 뒤 kind=unknown 으로 적재한다. 원문은 30일 뒤 지워지므로 버리지 않는다)."""
    t = o.get("type")
    if t in EXCLUDE_TYPES:
        return False, EXCLUDE_TYPES[t]
    if t not in KEEP_TYPES:
        return True, "unknown_type:%s" % t
    if t == "system":
        st = o.get("subtype")
        if st in EXCLUDE_SYSTEM:
            return False, EXCLUDE_SYSTEM[st]
    if t == "attachment":
        a = o.get("attachment") or {}
        at = a.get("type")
        if at in EXCLUDE_ATT:
            return False, EXCLUDE_ATT[at]
        if at not in KEEP_ATT:
            return True, "unknown_attachment:%s" % at
    return True, None


# ---- 정제 ---------------------------------------------------------------------
def _jlen(v):
    try:
        return len(json.dumps(v, ensure_ascii=False))
    except (TypeError, ValueError):
        return 0


def _bump(strips, reason, nbytes):
    n, b = strips.get(reason, (0, 0))
    strips[reason] = (n + 1, b + nbytes)


def _strip_image(block, strips):
    src = block.get("source") or {}
    data = src.get("data")
    if isinstance(data, str) and len(data) > 0:
        mt = src.get("media_type") or "?"
        _bump(strips, "image_base64", len(data))
        nb = src.copy()
        nb["data"] = "[이미지 생략 base64 %d바이트 %s]" % (len(data), mt)
        block = dict(block)
        block["source"] = nb
    return block


def _strip_content(content, strips):
    if not isinstance(content, list):
        return content
    out = []
    for b in content:
        if not isinstance(b, dict):
            out.append(b)
            continue
        bt = b.get("type")
        if bt == "image":
            b = _strip_image(b, strips)
        elif bt == "tool_result":
            cc = b.get("content")
            if isinstance(cc, list):
                b = dict(b)
                b["content"] = [(_strip_image(x, strips) if isinstance(x, dict) and x.get("type") == "image" else x)
                                for x in cc]
        elif bt == "thinking":
            sig = b.get("signature")
            if isinstance(sig, str) and sig:
                _bump(strips, "thinking_signature", len(sig))
                b = dict(b)
                b.pop("signature", None)
        elif bt == "redacted_thinking":
            d = b.get("data")
            if isinstance(d, str) and d:
                _bump(strips, "thinking_signature", len(d))
                b = {"type": "redacted_thinking"}
        out.append(b)
    return out


TUR_KEEP_MAX = 300


def _reduce_tur(tur, strips):
    """toolUseResult 는 message 의 tool_result 와 겹치는 사본이다. 짧은 스칼라만 남긴다."""
    if isinstance(tur, dict):
        keep = {}
        for k, v in tur.items():
            if isinstance(v, (bool, int, float)) or v is None:
                keep[k] = v
            elif isinstance(v, str) and len(v) <= TUR_KEEP_MAX:
                keep[k] = v
        dropped = _jlen(tur) - _jlen(keep)
        if dropped > 0:
            _bump(strips, "toolUseResult_copy", dropped)
        return keep
    n = _jlen(tur)
    if n > 2:
        _bump(strips, "toolUseResult_copy", n)
    return {"_dropped": type(tur).__name__}


def reduce_line(o, strips):
    """지식이 아닌 큰 필드를 자리표시로 바꾼 새 객체를 돌려준다(원본 객체는 건드리지 않는다)."""
    o = dict(o)
    t = o.get("type")
    for k in ("wireToolInputs", "rendered", "serverClassifierRequest", "serverClassifierContext", "wireIngestContext"):
        if k in o:
            _bump(strips, "dup:" + k, _jlen(o[k]))
            o.pop(k)
    if "toolUseResult" in o and o["toolUseResult"] is not None:
        o["toolUseResult"] = _reduce_tur(o["toolUseResult"], strips)
    m = o.get("message")
    if isinstance(m, dict):
        m = dict(m)
        if "content" in m:
            m["content"] = _strip_content(m["content"], strips)
        u = m.get("usage")
        if isinstance(u, dict) and "iterations" in u:
            u = dict(u)
            _bump(strips, "dup:usage.iterations", _jlen(u.pop("iterations")))
            m["usage"] = u
        o["message"] = m
    if t == "attachment":
        a = dict(o.get("attachment") or {})
        if a.get("type") == "invoked_skills" and isinstance(a.get("skills"), list):
            sk = []
            for s in a["skills"]:
                if isinstance(s, dict):
                    body = s.get("content")
                    if isinstance(body, str):
                        _bump(strips, "skill_body", len(body))
                    sk.append({k: v for k, v in s.items() if k != "content"})
            a["skills"] = sk
        o["attachment"] = a
    return o


# ---- 분류 ---------------------------------------------------------------------
def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _image_ph(b):
    src = b.get("source") or {}
    d = src.get("data")
    if isinstance(d, str) and d.startswith("[이미지"):
        return d
    return "[이미지]"


def user_text(content, include_images=True):
    parts = []
    for b in _blocks(content):
        bt = b.get("type")
        if bt == "text":
            parts.append(b.get("text") or "")
        elif bt == "image" and include_images:
            parts.append(_image_ph(b))
    return "\n".join(p for p in parts if p)


_ARGS_RX = re.compile(r"<command-args>([\s\S]*?)</command-args>")
_NAME_RX = re.compile(r"<command-name>([\s\S]*?)</command-name>")
_BARE_SLASH_RX = re.compile(r"^(/[A-Za-z][\w:-]*)(?=\s|$)")


def classify(o):
    """(kind, sub). 카드 시작 후보 kind: human, sdk, bash_in, slash, queued, qo_human."""
    t = o.get("type")
    side = bool(o.get("isSidechain"))
    if t == "user":
        m = o.get("message") or {}
        c = m.get("content")
        bl = _blocks(c)
        if o.get("isCompactSummary"):
            return "compact", None
        if o.get("toolUseResult") is not None or any(b.get("type") == "tool_result" for b in bl):
            return "tool_result", None
        if o.get("isMeta"):
            return "meta", None
        txt = user_text(c, include_images=False)
        origin = (o.get("origin") or {}).get("kind")
        ps = o.get("promptSource")
        if side:
            return "side_user", ps
        if txt.startswith(INTERRUPT_MARK):
            return "interrupt", None
        if origin == "human" or ps in ("typed", "queued", "suggestion_accepted"):
            return "human", ps or origin
        if ps == "sdk":
            return "sdk", ps
        if origin == "task-notification" or txt.startswith("<task-notification>"):
            return "task_note", origin
        if origin in ("peer", "coordinator") or txt.startswith("Another Claude session sent a message") \
                or txt.startswith("<teammate-message"):
            return "peer", origin
        if txt.startswith("<bash-input>"):
            return "bash_in", None
        if txt.startswith("<command-name>"):
            am = _ARGS_RX.search(txt)
            if am and am.group(1).strip():
                return "slash", (_NAME_RX.search(txt).group(1).strip() if _NAME_RX.search(txt) else None)
            return "command", None
        if txt.startswith("<local-command-") or txt.startswith("<bash-stdout>") or txt.startswith("<bash-stderr>"):
            return "command_out", None
        if not origin:
            # 감싸개(<command-name>) 없이 문자열로 적힌 슬래시 명령(실측: '/compact' 8줄, origin·promptSource 없음).
            # 경로('/Users/…')는 이름 뒤가 공백·끝이 아니라 걸리지 않는다.
            bare = txt.strip()
            bm = _BARE_SLASH_RX.match(bare)
            if bm:
                return ("slash", bm.group(1)) if bare[bm.end():].strip() else ("command", None)
        return "user_other", ps or origin
    if t == "assistant":
        m = o.get("message") or {}
        if o.get("isApiErrorMessage"):
            return "assistant", "api_error"
        kinds = []
        for b in _blocks(m.get("content")):
            bt = b.get("type")
            if bt and bt not in kinds:
                kinds.append(bt)
        return "assistant", "+".join(kinds) or None
    if t == "system":
        return "system", o.get("subtype")
    if t == "attachment":
        a = o.get("attachment") or {}
        at = a.get("type")
        if at == "queued_command" and a.get("commandMode") == "prompt" and (a.get("origin") or {}).get("kind") == "human" \
                and not side:
            return "queued", at
        return "att", at
    if t == "queue-operation":
        op = o.get("operation")
        c = o.get("content")
        if op == "enqueue" and isinstance(c, str) and c.strip() and not c.lstrip().startswith("<"):
            return "qo_human", op
        return "qo", op
    return t, None


CARD_STARTERS = {"human", "sdk", "bash_in", "slash", "queued"}


# ---- 추출 ---------------------------------------------------------------------
def _cap(s, n):
    if s is None:
        return None
    s = str(s)
    return s if len(s) <= n else s[:n]


def first_line(s, n=160):
    if not s:
        return ""
    for ln in str(s).splitlines():
        ln = ln.strip()
        if ln:
            return ln[:n]
    return ""


_TAG_RX = re.compile(r"</?[a-z_]+(?:-[a-z_]+)*>")
_EXIT_RX = re.compile(r"^Exit code \d+\s*$")
_EXC_RX = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning)\b")


def error_line(text):
    """에러 서명에 쓸 첫 줄. <tool_use_error> 같은 태그를 벗기고, 'Exit code N' 줄이면 다음 줄을 쓴다."""
    if not text:
        return ""
    lines = [ln.strip() for ln in _TAG_RX.sub("", str(text)).splitlines()]
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    prefix = ""
    if _EXIT_RX.match(lines[0]):
        if len(lines) == 1:
            return lines[0]
        prefix = "exit: "
        lines = lines[1:]
    if lines[0].startswith("Traceback (most recent call last)"):
        # 파이썬 역추적은 첫 줄이 늘 같으므로 마지막 예외 줄을 쓴다.
        for ln in reversed(lines):
            if _EXC_RX.match(ln):
                return prefix + "Traceback: " + ln
        return prefix + "Traceback: " + lines[-1]
    return prefix + lines[0]


_NORM = [
    (re.compile(r"\[REDACTED:[a-z_]+\]"), "<r>"),
    (re.compile(r"(?:[A-Za-z]:)?(?:~|\.{1,2})?(?:/[^\s:'\"()\[\],;]+)+/?"), "<path>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b|\b[0-9a-f]{7,}\b"), "<hex>"),
    (re.compile(r"\d+"), "<n>"),
    (re.compile(r"\"[^\"]*\""), "\"<q>\""),
    (re.compile(r"'[^']*'"), "'<q>'"),
    (re.compile(r"`[^`]*`"), "`<q>`"),
    (re.compile(r"\s+"), " "),
]


def normalize_error(line):
    s = line or ""
    for rx, rep in _NORM:
        s = rx.sub(rep, s)
    return s.strip()[:200]


def error_sig(line):
    n = normalize_error(line)
    if not n:
        return None
    return hashlib.sha1(n.encode("utf-8")).hexdigest()[:12]


_WF_NAME_RX = re.compile(r"name\s*:\s*['\"]([^'\"]{1,80})['\"]")


def tool_input_summary(name, inp):
    """도구 입력에서 카드·검색에 쓸 필드만 뽑는다(가림 뒤 값)."""
    if not isinstance(inp, dict):
        return {"_raw": _cap(json.dumps(inp, ensure_ascii=False), 1000)}
    d = {}
    for k in ("command", "description", "file_path", "notebook_path", "subagent_type", "name", "pattern", "path",
              "url", "query", "skill", "args", "task_id", "run_in_background", "model", "to", "summary"):
        if k in inp and inp[k] is not None:
            v = inp[k]
            d[k] = _cap(v, 2000) if isinstance(v, str) else v
    for k, n in (("prompt", 1500), ("content", 1500), ("old_string", 500), ("new_string", 500), ("message", 800),
                 ("new_source", 800)):
        if isinstance(inp.get(k), str):
            d[k] = _cap(inp[k], n)
    if name in TODO_TOOLS and isinstance(inp.get("todos"), list):
        d["todos"] = [{"content": _cap((t or {}).get("content"), 300), "status": (t or {}).get("status")}
                      for t in inp["todos"] if isinstance(t, dict)]
    if name in WORKFLOW_TOOLS:
        sc = inp.get("script")
        if isinstance(sc, str):
            m = _WF_NAME_RX.search(sc)
            if m:
                d["workflow_name"] = m.group(1)
            d["script_head"] = _cap(sc, 500)
        if inp.get("scriptPath"):
            d["scriptPath"] = inp.get("scriptPath")
    if name == "MultiEdit" and isinstance(inp.get("edits"), list):
        d["edits"] = len(inp["edits"])
    if not d:
        d["_raw"] = _cap(json.dumps(inp, ensure_ascii=False), 1000)
    return d


def tool_input_text(name, summ):
    parts = ["[%s]" % name]
    for k in ("description", "command", "file_path", "notebook_path", "pattern", "path", "url", "query", "skill",
              "args", "subagent_type", "name", "workflow_name", "prompt", "content", "old_string", "new_string",
              "message", "summary", "_raw"):
        v = summ.get(k)
        if v not in (None, ""):
            parts.append(str(v))
    if summ.get("todos"):
        parts.extend("- [%s] %s" % (t.get("status"), t.get("content")) for t in summ["todos"])
    return "\n".join(parts)


def _tr_text(b):
    cc = b.get("content")
    if isinstance(cc, str):
        return cc
    parts = []
    for x in cc or []:
        if isinstance(x, dict):
            if x.get("type") == "text":
                parts.append(x.get("text") or "")
            elif x.get("type") == "image":
                parts.append(_image_ph(x))
            elif x.get("type") == "tool_reference":
                parts.append("[tool_reference %s]" % (x.get("tool_name") or ""))
    return "\n".join(parts)


_AGENT_ID_RX = re.compile(r"agentId:\s*([0-9a-z]{8,32})")
_RUN_ID_RX = re.compile(r"\b(wf_[0-9a-f]{8}-[0-9a-f]{3})\b")


def extract(o, kind, sub):
    """가림이 끝난 객체에서 열 값을 뽑는다."""
    t = o.get("type")
    out = {"text": "", "msg_id": None, "model": None, "usage": None, "tool_uses": [], "tool_results": [],
           "sections": None, "title": None}
    if kind == "unknown":
        out["text"] = _cap(json.dumps(o, ensure_ascii=False), 20000)
        return out
    if t == "user":
        m = o.get("message") or {}
        c = m.get("content")
        if kind == "tool_result":
            tur = o.get("toolUseResult") if isinstance(o.get("toolUseResult"), dict) else {}
            texts = []
            for b in _blocks(c):
                if b.get("type") != "tool_result":
                    if b.get("type") == "text" and b.get("text"):
                        texts.append(b.get("text"))
                    continue
                s = _tr_text(b)
                texts.append(s)
                is_err = bool(b.get("is_error"))
                eline = error_line(s) if is_err else None
                denial = o.get("toolDenialKind")
                if not denial:
                    head = s[:300]
                    for mk in DENIAL_MARKERS:
                        if mk in head:
                            denial = "text"
                            break
                intr = 1 if (tur.get("interrupted") is True or "[Request interrupted by user for tool use]" in s[:400]) else 0
                link = tur.get("agentId") or tur.get("runId")
                if not link:
                    mm = _AGENT_ID_RX.search(s[:600]) or _RUN_ID_RX.search(s[:1200])
                    link = mm.group(1) if mm else None
                out["tool_results"].append({
                    "tuid": b.get("tool_use_id"), "is_err": 1 if is_err else 0,
                    "err_line": _cap(eline, 300) if is_err else None,
                    "err_sig": error_sig(eline) if is_err else None,
                    "denial": denial, "intr": intr, "link": link,
                })
            out["text"] = "\n".join(x for x in texts if x)
        else:
            out["text"] = user_text(c)
            if kind == "compact":
                out["sections"] = parse_compact_sections(out["text"])
    elif t == "assistant":
        m = o.get("message") or {}
        out["msg_id"] = m.get("id")
        out["model"] = m.get("model")
        u = m.get("usage")
        if isinstance(u, dict):
            out["usage"] = (int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                            int(u.get("cache_creation_input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0))
        parts = []
        for i, b in enumerate(_blocks(m.get("content"))):
            bt = b.get("type")
            if bt == "text":
                parts.append(b.get("text") or "")
            elif bt == "thinking":
                th = b.get("thinking") or ""
                if th.strip():
                    parts.append("[생각] " + th)
            elif bt == "tool_use":
                name = b.get("name") or "?"
                summ = tool_input_summary(name, b.get("input"))
                fpath = None
                if name in CHANGE_TOOLS:
                    fpath = summ.get("file_path") or summ.get("notebook_path")
                cmd = first_line(summ.get("command"), 160) if name == "Bash" else None
                out["tool_uses"].append({"tuid": b.get("id"), "idx": i, "name": name, "fpath": fpath, "cmd": cmd,
                                         "inp": json.dumps(summ, ensure_ascii=False)})
                parts.append(tool_input_text(name, summ))
        out["text"] = "\n".join(p for p in parts if p)
    elif t == "system":
        c = o.get("content")
        out["text"] = c if isinstance(c, str) else ""
        if o.get("subtype") == "turn_duration":
            out["text"] = ""
    elif t == "attachment":
        a = o.get("attachment") or {}
        at = a.get("type")
        if at == "queued_command":
            p = a.get("prompt")
            out["text"] = p if isinstance(p, str) else user_text(p)
        elif at == "edited_text_file":
            out["text"] = "%s\n%s" % (a.get("filename") or "", a.get("snippet") or "")
        elif at == "file":
            fc = a.get("content") or {}
            fl = fc.get("file") if isinstance(fc, dict) else None
            body = (fl or {}).get("content") if isinstance(fl, dict) else None
            out["text"] = "%s\n%s" % (a.get("filename") or "", body or "")
        elif at == "goal_status":
            out["text"] = "목표: %s (met=%s)" % (a.get("condition"), a.get("met"))
        elif at == "invoked_skills":
            out["text"] = "스킬: " + ", ".join(str((s or {}).get("name")) for s in a.get("skills") or [])
        elif at == "inlined_image_paths":
            out["text"] = "\n".join(str(p) for p in a.get("paths") or [])
        elif at == "compact_file_reference":
            out["text"] = str(a.get("filename") or "")
        else:
            out["text"] = _cap(json.dumps({k: v for k, v in a.items() if k != "type"}, ensure_ascii=False), 2000)
    elif t == "queue-operation":
        c = o.get("content")
        out["text"] = c if isinstance(c, str) else ""
    elif t in ("ai-title", "custom-title"):
        out["title"] = o.get("aiTitle") or o.get("customTitle") or o.get("title")
        out["text"] = out["title"] or ""
    elif t == "history":
        out["text"] = o.get("display") or ""
    elif t == "summary":
        out["text"] = o.get("summary") or ""
    elif t == "frame-link":
        out["text"] = " ".join(str(o.get(k) or "") for k in ("title", "path", "frameUrl")).strip()
    elif t in ("launched", "started", "result"):
        out["text"] = _cap(json.dumps({k: v for k, v in o.items() if k not in ("type",)}, ensure_ascii=False), 20000)
    elif t == "permission-mode":
        out["text"] = str(o.get("permissionMode") or "")
    elif t == "mode":
        out["text"] = str(o.get("mode") or "")
    elif t == "agent-setting":
        out["text"] = str(o.get("agentSetting") or "")
    return out


# ---- 압축 요약 절 나누기 ------------------------------------------------------
_H_MD = re.compile(r"^#{1,4}\s*(\d{1,2})\.\s+(.+?)\s*$")
_H_COLON = re.compile(r"^(\d{1,2})\.\s+([A-Z][A-Za-z0-9 &/,'-]{2,60}?)(?:\s*\([^)]{0,60}\))?\s*:\s*$")


def parse_compact_sections(text):
    """압축 요약을 절로 나눈다. '## N. 제목' 형식과 'N. 제목:' 형식을 모두 읽는다.
    번호가 1 부터 하나씩 늘어나는 줄만 절 머리로 본다(본문의 번호 목록과 구분)."""
    if not text:
        return []
    lines = text.split("\n")
    fmt = None
    for ln in lines:
        if _H_MD.match(ln.strip()):
            fmt = "md"
            break
    if fmt is None:
        fmt = "colon"
    rx = _H_MD if fmt == "md" else _H_COLON
    heads = []
    expect = 1
    for i, ln in enumerate(lines):
        mm = rx.match(ln.strip() if fmt == "md" else ln.rstrip())
        if mm and int(mm.group(1)) == expect:
            heads.append((i, int(mm.group(1)), mm.group(2).strip().rstrip(":")))
            expect += 1
    out = []
    for j, (i, n, title) in enumerate(heads):
        end = heads[j + 1][0] if j + 1 < len(heads) else len(lines)
        body = "\n".join(lines[i + 1:end]).strip()
        out.append({"n": n, "title": title, "body": body, "fmt": fmt})
    return out


def line_sha(raw):
    return hashlib.sha1(raw).hexdigest()


def fts_body(kind, sub, text, tool_uses=None):
    """FTS 에 넣을 본문(가림 뒤). 상한은 여기서만 자른다. None 이면 색인하지 않는다."""
    if not text:
        return None
    if kind in ("human", "sdk", "bash_in", "slash", "queued", "qo_human", "compact", "history"):
        return text
    if kind == "assistant":
        return text if len(text) <= 20000 else text[:20000]
    if kind == "tool_result":
        return text if len(text) <= 3000 else text[:2500] + "\n…\n" + text[-500:]
    if kind == "system":
        if sub in ("away_summary", "informational", "local_command", "api_error", "model_refusal_fallback",
                   "scheduled_task_fire", "compact_boundary"):
            return text[:4000]
        return None
    if kind == "att":
        if sub in ("edited_text_file", "file", "goal_status", "structured_output", "task_reminder"):
            return text[:3000]
        return None
    if kind in ("interrupt", "task_note", "peer", "user_other", "command_out", "side_user", "meta"):
        if kind in ("meta",):
            return None
        return text[:3000]
    if kind in ("result", "started", "launched", "unknown"):
        return text[:4000]
    if kind in ("frame-link", "summary"):
        return text[:1000]
    return None
