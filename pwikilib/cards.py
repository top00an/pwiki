"""결정론 작업 카드. 사람 입력 하나마다 한 장이고, 다음 사람 입력 직전까지의 사실만 모은다. LLM 요약은 없다.

사람 입력(카드 시작)
- type:user 사람 줄(origin human, promptSource typed·queued·suggestion_accepted), sdk 프롬프트(claude -p, 자동 실행 묶음),
  '!' 셸 입력(<bash-input>), 인자가 있는 슬래시 명령. 인자 없는 슬래시 명령(/model 등)은 카드가 아니고 day 에서 따로 센다.
- 작업 도중 입력: attachment/queued_command(commandMode=prompt, origin human)
- 작업 도중 '/goal <글>': 사람 줄이 남지 않고 attachment/queued_command(origin auto-continuation, 글 'Goal set: <글>')로
  전달된다(history.jsonl 은 같은 ms 에 '/goal <글>' 을 적는다). 이 첨부를 사람 입력 카드(human_kind=goal)로 본다.
  한가할 때 친 '/goal <글>' 은 <command-name> 사람 줄(slash)로 남고 이 첨부가 없다(실측).
- queue-operation enqueue: 같은 글(자리표시·태그 정규화 뒤)의 줄이 뒤에 있거나 2분 안 앞에 있으면 그쪽이 카드다.
  짝이 없을 때만 따로 한 장(delivered=0, 뒤 사건을 끌어오지 않음, '전달 안 된 대기열').
- 사람 입력이 오기 전의 일(팀원 세션의 team-lead 메시지, 다른 세션 메시지 등): '사람 입력 없음' 카드 한 장
  (human_kind=no_human). 세션의 토큰·에러가 빠지지 않게 한다.
토큰: message.id 하나는 세션 안에서 처음 나온 카드 하나에만 넣는다(줄마다 반복되는 usage 중 출력이 가장 큰 줄 기준).
하위 에이전트·워크플로 토큰: agentId·runId 하나는 처음 띄운 카드 하나에만 넣는다(재개한 Workflow 호출은 연결만).
카드 키: c:<sessionId>:<첫 사람 줄 uuid>. uuid 가 없으면 그 줄의 결정론 키를 쓴다.
"""
import collections
import json
import re

from . import parse
from . import paths

VERIFY_RX = re.compile(r"검증|검토|리뷰|감사|verif|review|audit|reviewer|check", re.I)
FEATURES = ["workflow", "subagent", "todo", "plan", "verify_agent", "skill"]
WORK_KINDS = {"assistant", "tool_result", "interrupt"}
WORK_SYSTEM = {"turn_duration", "api_error", "model_refusal_fallback", "compact_boundary"}


def _norm(s):
    return " ".join((s or "").split())[:500]


_IMG_RX = re.compile(r"\[이미지[^\]]*\]|\[Image #\d+\]")
_BASH_RX = re.compile(r"<bash-input>([\s\S]*?)</bash-input>")
_CMD_NAME_RX = re.compile(r"<command-name>([\s\S]*?)</command-name>")
_CMD_ARGS_RX = re.compile(r"<command-args>([\s\S]*?)</command-args>")
QO_MATCH_KINDS = {"human", "sdk", "user_other", "task_note", "peer", "interrupt", "bash_in", "slash", "command",
                  "command_out", "queued", "meta"}
QO_BEFORE_S = 120
GOAL_PREFIX = "Goal set: "


def is_goal_input(kind, sub, text):
    """작업 도중 '/goal <글>' 의 전달 줄(attachment/queued_command 'Goal set: <글>')인가."""
    return kind == "att" and sub == "queued_command" and (text or "").startswith(GOAL_PREFIX) \
        and len((text or "")[len(GOAL_PREFIX):].strip()) > 0


def match_key(kind, text):
    """대기열 짝 찾기용 정규화: 이미지 자리표시([이미지…]·[Image #N])를 지우고, '!' 입력은 <bash-input> 안쪽,
    슬래시 명령은 '/이름 인자' 로 맞춘다."""
    t = text or ""
    if kind == "bash_in":
        m = _BASH_RX.search(t)
        t = m.group(1) if m else t
    elif kind in ("slash", "command") or t.startswith("<command-name>"):
        n = _CMD_NAME_RX.search(t)
        a = _CMD_ARGS_RX.search(t)
        if n:
            t = n.group(1).strip() + (" " + a.group(1).strip() if a and a.group(1).strip() else "")
    t = _IMG_RX.sub(" ", t)
    t = " ".join(t.split())
    if t.startswith("!"):
        t = t[1:].lstrip()
    return t[:300]


def _tokens_by_fid(con, fids):
    """파일마다 assistant 토큰 합(message.id 당 한 번, 출력이 가장 큰 줄 기준)."""
    out = {}
    for fid in fids:
        best = {}
        for mid, ui, uo, ucc, ucr in con.execute(
                "SELECT msg_id, u_in, u_out, u_cc, u_cr FROM events WHERE fid=? AND type='assistant' AND msg_id IS NOT NULL",
                (fid,)):
            cur = best.get(mid)
            if cur is None or (uo or 0) > (cur[1] or 0):
                best[mid] = (ui or 0, uo or 0, ucc or 0, ucr or 0)
        t = [0, 0, 0, 0]
        for v in best.values():
            for i in range(4):
                t[i] += v[i]
        out[fid] = t
    return out


def build_sessions(con, sids):
    n_cards = 0
    n_sess = 0
    for sid in sids:
        con.execute("BEGIN IMMEDIATE")
        try:
            n = build_session(con, sid)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        n_cards += n
        n_sess += 1
    return {"sessions": n_sess, "cards": n_cards}


def _fts_put(con, key, body, kind, project, ts, sid):
    r = con.execute("SELECT rid FROM fts_map WHERE key=?", (key,)).fetchone()
    if r:
        con.execute("DELETE FROM fts WHERE rowid=?", (r[0],))
        con.execute("DELETE FROM fts_map WHERE key=?", (key,))
    if body:
        cur = con.execute("INSERT INTO fts(body, key, kind, project, ts, sid) VALUES (?,?,?,?,?,?)",
                          (body, key, kind, project, ts, sid))
        con.execute("INSERT INTO fts_map(key, rid) VALUES (?,?)", (key, cur.lastrowid))


def build_session(con, sid):
    """세션 하나의 카드를 지우고 다시 만든다(결정론이라 같은 입력이면 같은 행)."""
    old = [r[0] for r in con.execute("SELECT key FROM cards WHERE sid=?", (sid,))]
    for k in old:
        _fts_put(con, k, None, None, None, None, None)
    con.execute("DELETE FROM cards WHERE sid=?", (sid,))
    all_cards, srow = compute_session(con, sid)
    if all_cards is None:
        con.execute("DELETE FROM sessions WHERE sid=?", (sid,))
        return 0
    for c in all_cards:
        _insert(con, c)
    con.execute("INSERT OR REPLACE INTO sessions(sid, project, cwd, title, first_ts, last_ts, n_cards, entry, built)"
                " VALUES (?,?,?,?,?,?,?,?,?)", srow)
    return len(all_cards)


def compute_session(con, sid):
    """세션 하나의 카드를 계산만 한다(DB 에 쓰지 않는다. 미리보기에도 쓴다). (카드 목록, sessions 행).
    세션 파일이 없으면 (None, None)."""
    mains = con.execute("SELECT fid, project FROM files WHERE sid=? AND kind='session' ORDER BY path", (sid,)).fetchall()
    if not mains:
        return None, None
    # 서브에이전트·워크플로 토큰 연결 자료
    subs = con.execute("SELECT fid, path, kind FROM files WHERE sid=? AND kind IN ('subagent','wf_agent')", (sid,)).fetchall()
    sub_tok = _tokens_by_fid(con, [s[0] for s in subs])
    tok_by_agent = {}
    tok_by_run = collections.defaultdict(lambda: [0, 0, 0, 0])
    for fid, path, kind in subs:
        base = path.rsplit("/", 1)[-1]
        aid = base[len("agent-"):-len(".jsonl")] if base.startswith("agent-") else None
        if kind == "subagent" and aid:
            tok_by_agent[aid] = sub_tok[fid]
        if kind == "wf_agent":
            m = re.search(r"/workflows/(wf_[^/]+)/", path)
            if m:
                t = tok_by_run[m.group(1)]
                for i in range(4):
                    t[i] += sub_tok[fid][i]
    agent_by_tuid = {}
    for (meta,) in con.execute("SELECT meta FROM docs WHERE sid=? AND kind='agent_meta'", (sid,)):
        try:
            mj = json.loads(meta or "{}")
        except ValueError:
            continue
        if mj.get("toolUseId") and mj.get("agentId"):
            agent_by_tuid[mj["toolUseId"]] = mj["agentId"]

    title = None
    first_ts = last_ts = None
    project = mains[0][1]
    cwd_any = None
    entry_any = None
    all_cards = []
    # message.id 별 최대 usage(세션 전체). 한 응답이 여러 줄로 나뉘고 usage 가 줄마다 반복된다.
    best_usage = {}
    for fid, _ in mains:
        for mid, ui, uo, ucc, ucr in con.execute(
                "SELECT msg_id, u_in, u_out, u_cc, u_cr FROM events WHERE fid=? AND type='assistant' AND msg_id IS NOT NULL",
                (fid,)):
            cur_u = best_usage.get(mid)
            if cur_u is None or (uo or 0) > cur_u[1]:
                best_usage[mid] = (ui or 0, uo or 0, ucc or 0, ucr or 0)
    sess = {"best": best_usage, "owner": {}, "claimed": set()}
    for fid, proj in mains:
        evs = con.execute("SELECT key, uuid, kind, sub, ts, text, msg_id, model, u_in, u_out, u_cc, u_cr, cwd, entry, ver,"
                          " off, CASE WHEN sub='turn_duration' THEN raw END FROM events WHERE fid=? ORDER BY off",
                          (fid,)).fetchall()
        tus = collections.defaultdict(list)
        for row in con.execute("SELECT ekey, tuid, name, fpath, cmd, inp FROM tool_uses WHERE fid=? ORDER BY off, idx", (fid,)):
            tus[row[0]].append(row[1:])
        trs = collections.defaultdict(list)
        for row in con.execute("SELECT ekey, tuid, is_err, err_sig, err_line, denial, intr, link FROM tool_results WHERE fid=?",
                               (fid,)):
            trs[row[0]].append(row[1:])
        # queue-operation 사람 입력의 짝 찾기: 같은 글이 뒤에(또는 2분 안 앞에) 사람 입력 줄로 나오면 그쪽이 카드다.
        texts = collections.defaultdict(list)
        for i, e in enumerate(evs):
            k, sub = e[2], e[3]
            if k in QO_MATCH_KINDS or (k == "att" and sub == "queued_command"):
                texts[match_key(k, e[5])].append(i)
        cards = []
        cur = None
        pre_input = None
        for i, e in enumerate(evs):
            key, uuid, kind, sub, ts, text = e[0], e[1], e[2], e[3], e[4], e[5]
            if ts:
                first_ts = ts if first_ts is None or ts < first_ts else first_ts
                last_ts = ts if last_ts is None or ts > last_ts else last_ts
            if e[12] and not cwd_any:
                cwd_any = e[12]
            if e[13] and not entry_any:
                entry_any = e[13]
            if kind in ("ai-title", "custom-title") and text:
                title = text
            if kind == "qo_human":
                if not _qo_paired(evs, i, texts.get(match_key("qo_human", text), [])):
                    cards.append(_new_card(sid, proj, e, delivered=0, seq=len(cards)))
                continue
            if kind in parse.CARD_STARTERS:
                cur = _new_card(sid, proj, e, delivered=1, seq=len(cards))
                cards.append(cur)
                continue
            if is_goal_input(kind, sub, text):
                cur = _new_card(sid, proj, e, delivered=1, seq=len(cards), human_kind="goal")
                cards.append(cur)
                continue
            if cur is None:
                # 사람 입력이 오기 전: 입력 성격의 줄은 기억해 두고, 일한 흔적이 처음 나오면 '사람 입력 없음' 카드를 연다.
                if kind in ("peer", "task_note", "user_other", "side_user", "command", "meta") and text:
                    if pre_input is None or (pre_input[2] == "meta" and kind != "meta"):
                        pre_input = e
                if kind in WORK_KINDS or (kind == "system" and sub in WORK_SYSTEM):
                    cur = _new_card(sid, proj, pre_input or e, delivered=1, seq=len(cards), no_human=True)
                    cards.append(cur)
                else:
                    continue
            _accumulate(cur, e, tus.get(key, []), trs.get(key, []), sess)
        all_cards.extend(cards)
    for c in all_cards:
        _finish(c, tok_by_agent, tok_by_run, agent_by_tuid, sess)
    return all_cards, (sid, project, cwd_any, title, first_ts, last_ts, len(all_cards), entry_any, paths.now_utc_iso())


def _qo_paired(evs, i, idxs):
    """대기열 입력 i 의 짝: 같은 정규화 글이 뒤에 있거나, 앞에 있되 2분 안이다."""
    ti = paths.parse_ts(evs[i][4])
    for j in idxs:
        if j > i:
            return True
        if j < i and ti is not None:
            tj = paths.parse_ts(evs[j][4])
            if tj is not None and abs((ti - tj).total_seconds()) <= QO_BEFORE_S:
                return True
    return False


def _new_card(sid, project, e, delivered, seq, no_human=False, human_kind=None):
    key, uuid, kind, sub, ts, text = e[0], e[1], e[2], e[3], e[4], e[5]
    ck = "c:%s:%s" % (sid, uuid) if uuid else "c:" + key
    if no_human:
        hk = "no_human"
        human = ("[%s] " % kind + (text or "")) if kind not in WORK_KINDS and kind != "system" else ""
    else:
        hk = human_kind or (kind if kind != "human" else "human:%s" % (sub or ""))
        human = text or ""
    return {
        "key": ck, "sid": sid, "project": project, "cwd": e[12], "first_uuid": uuid, "first_ekey": key,
        "human_kind": hk, "delivered": delivered,
        "start_ts": ts, "end_ts": ts, "human": human, "last_asst": None,
        "msgs": {}, "msg_model": {}, "models": collections.Counter(), "tools": collections.Counter(),
        "files": [], "cmds": [], "sigs": collections.Counter(), "sig_line": {}, "n_err": 0,
        "n_denied": 0, "n_intr": 0, "turn_ms": 0, "n_sub": 0, "n_wf": 0, "sub_tuids": [], "wf_links": [],
        "feat": {f: 0 for f in FEATURES}, "todos": None, "entry": e[13], "ver": e[14], "seq": seq, "n_events": 1,
        "modes": collections.Counter(), "err_last": None,
    }


def _accumulate(c, e, tus, trs, sess):
    key, uuid, kind, sub, ts, text = e[0], e[1], e[2], e[3], e[4], e[5]
    c["n_events"] += 1
    # 끝 시각은 일한 흔적(답·도구 결과·중단·turn_duration)으로만 잡는다. 자리 비움 요약 같은 줄은 몇 시간 뒤에 붙어 소요를 부풀린다.
    work = kind in WORK_KINDS or (kind == "system" and sub in WORK_SYSTEM)
    if ts and work and (c["end_ts"] is None or ts > c["end_ts"]):
        c["end_ts"] = ts
    if not c["cwd"] and e[12]:
        c["cwd"] = e[12]
    if kind == "assistant":
        mid = e[6]
        if mid:
            owner = sess["owner"].setdefault(mid, c["key"])
            if owner == c["key"]:
                c["msgs"][mid] = sess["best"].get(mid, (e[8] or 0, e[9] or 0, e[10] or 0, e[11] or 0))
                if e[7] and e[7] != "<synthetic>" and mid not in c["msg_model"]:
                    c["msg_model"][mid] = e[7]
                    c["models"][e[7]] += 1
        if sub == "text" and text:
            c["last_asst"] = text
    elif kind == "interrupt":
        c["n_intr"] += 1
    elif kind == "system" and sub == "turn_duration":
        try:
            c["turn_ms"] += int(json.loads(e[16]).get("durationMs") or 0)
        except (ValueError, TypeError):
            pass
    elif kind == "permission-mode" and text:
        c["modes"][text] += 1
        if text == "plan":
            c["feat"]["plan"] = 1
    elif kind == "mode" and text == "plan":
        c["feat"]["plan"] = 1
    for tuid, name, fpath, cmd, inp in tus:
        c["tools"][name] += 1
        if fpath and fpath not in c["files"]:
            c["files"].append(fpath)
        if cmd and len(c["cmds"]) < 40 and cmd not in c["cmds"]:
            c["cmds"].append(cmd)
        try:
            ij = json.loads(inp or "{}")
        except ValueError:
            ij = {}
        if name in parse.SUBAGENT_TOOLS:
            c["n_sub"] += 1
            c["feat"]["subagent"] = 1
            c["sub_tuids"].append(tuid)
            if VERIFY_RX.search(" ".join(str(ij.get(k) or "") for k in ("description", "subagent_type", "name"))):
                c["feat"]["verify_agent"] = 1
        elif name in parse.WORKFLOW_TOOLS:
            c["n_wf"] += 1
            c["feat"]["workflow"] = 1
            if VERIFY_RX.search(str(ij.get("workflow_name") or "")):
                c["feat"]["verify_agent"] = 1
            c["wf_links"].append(tuid)
        elif name in parse.TODO_TOOLS:
            c["feat"]["todo"] = 1
            if isinstance(ij.get("todos"), list):
                c["todos"] = ij["todos"]
        elif name in parse.PLAN_TOOLS:
            c["feat"]["plan"] = 1
        elif name == "Skill":
            c["feat"]["skill"] = 1
    for tuid, is_err, sig, eline, denial, intr, link in trs:
        if is_err:
            c["n_err"] += 1
            if sig:
                c["sigs"][sig] += 1
                c["sig_line"].setdefault(sig, eline)
            c["err_last"] = (sig, eline, tuid)
        if denial:
            c["n_denied"] += 1
        if intr:
            c["n_intr"] += 1
        if link:
            c.setdefault("links", {})[tuid] = link


def _finish(c, tok_by_agent, tok_by_run, agent_by_tuid, sess):
    t = [0, 0, 0, 0]
    for v in c["msgs"].values():
        for i in range(4):
            t[i] += v[i]
    c["tok"] = t
    sub = [0, 0, 0, 0]
    links = c.get("links", {})
    claimed = sess["claimed"]
    # agentId·runId 하나는 처음 띄운 카드에만(재개한 Workflow·같은 에이전트 이어 쓰기는 연결만 하고 토큰은 넣지 않는다)
    for tuid in c["sub_tuids"]:
        aid = agent_by_tuid.get(tuid) or links.get(tuid)
        if aid and aid in tok_by_agent and ("a", aid) not in claimed:
            claimed.add(("a", aid))
            for i in range(4):
                sub[i] += tok_by_agent[aid][i]
    for tuid in c["wf_links"]:
        rid = links.get(tuid)
        if rid and rid in tok_by_run and ("r", rid) not in claimed:
            claimed.add(("r", rid))
            for i in range(4):
                sub[i] += tok_by_run[rid][i]
    c["sub_tok"] = sub
    c["model"] = c["models"].most_common(1)[0][0] if c["models"] else None
    d0 = paths.parse_ts(c["start_ts"])
    d1 = paths.parse_ts(c["end_ts"])
    c["dur_s"] = max(0.0, (d1 - d0).total_seconds()) if d0 and d1 else None


def _insert(con, c):
    sigs = [[s, n, c["sig_line"].get(s)] for s, n in c["sigs"].most_common()]
    n_rep = sum(n - 1 for n in c["sigs"].values() if n > 1)
    feats = dict(c["feat"])
    if c.get("modes"):
        feats["permission_modes"] = sorted(c["modes"])
    row = (c["key"], c["sid"], c["project"], c["cwd"], c["first_uuid"], c["first_ekey"], c["human_kind"], c["delivered"],
           c["start_ts"], c["end_ts"], c["dur_s"], c["turn_ms"], c["human"], c["last_asst"],
           len(c["msgs"]), sum(c["tools"].values()), json.dumps(dict(c["tools"].most_common()), ensure_ascii=False),
           json.dumps(c["files"], ensure_ascii=False), len(c["files"]), json.dumps(c["cmds"], ensure_ascii=False),
           c["n_err"], json.dumps(sigs, ensure_ascii=False), n_rep, c["n_denied"], c["n_intr"],
           c["tok"][0], c["tok"][1], c["tok"][2], c["tok"][3],
           c["n_sub"], c["n_wf"], c["sub_tok"][1], sum(c["sub_tok"]),
           json.dumps(feats, ensure_ascii=False), c["model"],
           json.dumps(c["todos"], ensure_ascii=False) if c["todos"] is not None else None,
           c["entry"], c["ver"], c["seq"], c["n_events"], paths.now_utc_iso())
    con.execute("INSERT OR REPLACE INTO cards(key, sid, project, cwd, first_uuid, first_ekey, human_kind, delivered,"
                " start_ts, end_ts, dur_s, turn_ms, human, last_asst, n_asst, n_tools, tools, files, n_files, cmds,"
                " n_err, err_sigs, n_err_repeat, n_denied, n_intr, tok_in, tok_out, tok_cc, tok_cr,"
                " n_sub, n_wf, sub_tok_out, sub_tok_all, features, model, todos, entry, ver, seq, n_events, built)"
                " VALUES (" + ",".join(["?"] * 41) + ")", row)
    body = card_search_body(c)
    _fts_put(con, c["key"], body, "card", c["project"], c["start_ts"], c["sid"])


def card_search_body(c):
    """카드 검색 본문. 근거성 숫자 줄(토큰·소요·횟수)은 넣지 않는다."""
    parts = [(c["human"] or "")[:2000]]
    if c.get("last_asst"):
        parts.append(c["last_asst"][:800])
    parts.extend(c["files"][:50])
    parts.extend(c["cmds"][:20])
    for s, line in c["sig_line"].items():
        if line:
            parts.append(line)
    return "\n".join(p for p in parts if p)
