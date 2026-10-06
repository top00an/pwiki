#!/usr/bin/python3
"""Append a single pwiki SessionStart hook to Claude Code settings.json and nothing else (existing entries are left as they are).

Usage:
  merge_settings.py                     preview (writes nothing; default)
  merge_settings.py --apply             actually append
  merge_settings.py --remove            preview removing only the appended pwiki entry (undo)
  merge_settings.py --remove --apply    actually remove
  --settings PATH   target (default ~/.claude/settings.json; if it is a symlink, the real file is written)
  --command CMD     hook command to append (or remove). The snippet is built from this command (matcher startup|clear, timeout 5)
  --python PATH     python used to build the command when --command is absent (default /usr/bin/python3, else the current python)
  --pwiki-home DIR  when --command is absent, prefix the command with PWIKI_HOME (omitted when it is the default ~/.pwiki)
  --snippet PATH    snippet JSON file (old method). Not used together with --command
  --print-snippet   print the built snippet JSON and exit (settings is not read)
  --script PATH     --remove only. Also remove any command that has this hook file path as an argument, even if the command text differs
                    (when removing without an install record, or after installing with a different PWIKI_HOME or python)
  Without a command, it is built as '<python> <this folder>/pwiki_session_start.py'.

Steps:
  1) Pre-merge validation: are the target and the snippet JSON objects, are there no duplicate keys, is the hooks structure valid,
     does the file referenced by the snippet's command exist, is it not already present.
  2) Merge in memory and check: exactly one group added at the end of hooks.SessionStart, every other value (key order included) unchanged,
     and the serialized text reads back as the same object.
  3) Write (only with --apply): temp file in the same folder → original permissions → right before replacing, confirm the original
     has not changed in the meantime → atomic replace.
  4) Post-write check: read back from disk and repeat the checks of 2). On failure, write back the original bytes read earlier.

Output never includes values from settings.json (only counts, event names and verdicts). No backup copy is made (undo is --remove).
Writing re-serializes the JSON. If the original is in json.dumps form (e.g. indent 2, the Claude Code default) the bytes come out identical;
if it was edited by hand, meaning and key order are kept but whitespace changes. Removal also deletes hooks.SessionStart and hooks
if they become empty.
Exit codes: 0 pass (changed, or already in that state), 3 validation failed (nothing written), 4 another write happened in the meantime
(nothing written), 5 post-write check failed (original bytes restored).
"""
import argparse
import copy
import json
import os
import shlex
import stat
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
EVENT = "SessionStart"
HOOK_SCRIPT = "pwiki_session_start.py"
MATCHER = "startup|clear"
TIMEOUT = 5


class Invalid(Exception):
    pass


def _no_dup(pairs):
    seen = set()
    for k, _ in pairs:
        if k in seen:
            raise Invalid("중복 키가 있다(키 이름: %s)" % k)
        seen.add(k)
    return dict(pairs)


def parse(text, what):
    try:
        obj = json.loads(text, object_pairs_hook=_no_dup)
    except Invalid:
        raise
    except ValueError as e:
        raise Invalid("%s 가 JSON 이 아니다(%d행 %d열)" % (what, getattr(e, "lineno", 0), getattr(e, "colno", 0)))
    if not isinstance(obj, dict):
        raise Invalid("%s 의 최상위가 객체가 아니다" % what)
    return obj


def canon(x):
    """키 순서·형(1 과 1.0 과 true)까지 가르는 비교용 글."""
    return json.dumps(x, ensure_ascii=False)


def check_hooks_shape(obj, what):
    hooks = obj.get("hooks")
    if hooks is None:
        return
    if not isinstance(hooks, dict):
        raise Invalid("%s 의 hooks 가 객체가 아니다" % what)
    for ev, groups in hooks.items():
        if not isinstance(groups, list):
            raise Invalid("%s 의 hooks.%s 가 배열이 아니다" % (what, ev))
        for g in groups:
            if not isinstance(g, dict) or not isinstance(g.get("hooks"), list):
                raise Invalid("%s 의 hooks.%s 에 hooks 배열이 없는 그룹이 있다" % (what, ev))
            for h in g["hooks"]:
                if not isinstance(h, dict):
                    raise Invalid("%s 의 hooks.%s 에 객체가 아닌 훅이 있다" % (what, ev))


def default_python():
    return "/usr/bin/python3" if os.path.isfile("/usr/bin/python3") else (sys.executable or "python3")


def make_command(python=None, pwiki_home=None, hook=None):
    """설치 경로·python 경로로 훅 명령을 만든다. PWIKI_HOME 이 기본(~/.pwiki)과 다를 때만 env 로 붙인다."""
    parts = []
    default_home = os.path.join(os.path.expanduser("~"), ".pwiki")
    if pwiki_home and os.path.abspath(pwiki_home) != os.path.abspath(default_home):
        parts += ["/usr/bin/env", "PWIKI_HOME=" + os.path.abspath(pwiki_home)]
    parts += [python or default_python(), hook or os.path.join(HERE, HOOK_SCRIPT)]
    return " ".join(shlex.quote(x) for x in parts)


def make_snippet(command):
    return {"hooks": {EVENT: [{"matcher": MATCHER, "hooks": [{"type": "command", "command": command,
                                                                "timeout": TIMEOUT}]}]}}


def load_snippet(path=None, command=None, check_files=True):
    if command is not None:
        sn = parse(json.dumps(make_snippet(command), ensure_ascii=False), "조각")
    else:
        with open(path, encoding="utf-8") as fh:
            sn = parse(fh.read(), "조각")
    check_hooks_shape(sn, "조각")
    if list(sn.keys()) != ["hooks"] or list(sn["hooks"].keys()) != [EVENT] or len(sn["hooks"][EVENT]) != 1:
        raise Invalid("조각은 hooks.%s 그룹 하나만 담아야 한다" % EVENT)
    group = sn["hooks"][EVENT][0]
    hs = group["hooks"]
    if len(hs) != 1 or hs[0].get("type") != "command" or not isinstance(hs[0].get("command"), str):
        raise Invalid("조각의 그룹은 command 훅 하나만 담아야 한다")
    cmd = hs[0]["command"]
    if check_files:  # 빼기(되돌리기)는 명령 글자만 맞추므로 파일이 사라졌어도 뺄 수 있게 한다
        for tok in shlex.split(cmd):
            if tok.startswith("/") and not os.path.isfile(tok):
                raise Invalid("조각의 명령이 가리키는 파일이 없다: %s" % tok)
    return group, cmd


def other_pwiki_hooks(obj, cmd):
    """명령은 다르지만 pwiki 이어 하기 훅으로 보이는 항목 수(다른 설치 위치의 훅). 두 번 붙지 않게 막는 데 쓴다."""
    n = 0
    for g in ((obj.get("hooks") or {}).get(EVENT)) or []:
        for h in g.get("hooks") or []:
            c = h.get("command") if isinstance(h, dict) else None
            if isinstance(c, str) and c != cmd and HOOK_SCRIPT in c:
                n += 1
    return n


def _runs_script(c, script):
    try:
        toks = shlex.split(c)
    except ValueError:
        return False
    rs = os.path.realpath(script)
    return any(t == script or (t.startswith("/") and os.path.realpath(t) == rs) for t in toks)


def our_hook(h, cmd, script=None):
    if not isinstance(h, dict) or not isinstance(h.get("command"), str):
        return False
    return h["command"] == cmd or bool(script and _runs_script(h["command"], script))


def find_ours(obj, cmd, script=None):
    """(그룹 번호, 훅 번호) 목록."""
    out = []
    for gi, g in enumerate(((obj.get("hooks") or {}).get(EVENT)) or []):
        for hi, h in enumerate(g.get("hooks") or []):
            if our_hook(h, cmd, script):
                out.append((gi, hi))
    return out


def add(orig, group):
    new = copy.deepcopy(orig)
    hooks = new.setdefault("hooks", {})
    hooks.setdefault(EVENT, []).append(copy.deepcopy(group))
    return new


def remove(orig, cmd, script=None):
    new = copy.deepcopy(orig)
    hooks = new.get("hooks") or {}
    groups = hooks.get(EVENT) or []
    kept = []
    for g in groups:
        hs = [h for h in g.get("hooks") or [] if not our_hook(h, cmd, script)]
        if len(hs) == len(g.get("hooks") or []):
            kept.append(g)
        elif hs:
            g2 = dict(g)
            g2["hooks"] = hs
            kept.append(g2)
    if EVENT in hooks:
        if kept:
            hooks[EVENT] = kept
        else:
            del hooks[EVENT]
    if "hooks" in new and not new["hooks"]:
        del new["hooks"]
    return new


def verify_add(orig, new, group):
    """합친 결과가 '끝에 그룹 하나 덧붙임'만인지. 어긋난 점 목록(빈 목록이면 통과)."""
    bad = []
    ok_top = list(orig.keys()) + ([] if "hooks" in orig else ["hooks"])
    if list(new.keys()) != ok_top:
        bad.append("최상위 키가 바뀌었다")
    for k in orig:
        if k != "hooks" and canon(orig[k]) != canon(new.get(k)):
            bad.append("최상위 %s 가 바뀌었다" % k)
    oh = orig.get("hooks") or {}
    nh = new.get("hooks") or {}
    want_ev = list(oh.keys()) + ([] if EVENT in oh else [EVENT])
    if list(nh.keys()) != want_ev:
        bad.append("hooks 의 이벤트 목록이 바뀌었다")
    for ev in oh:
        if ev != EVENT and canon(oh[ev]) != canon(nh.get(ev)):
            bad.append("hooks.%s 가 바뀌었다" % ev)
    og = oh.get(EVENT) or []
    ng = nh.get(EVENT) or []
    if len(ng) != len(og) + 1:
        bad.append("hooks.%s 그룹 수가 %d → %d 다(하나만 늘어야 한다)" % (EVENT, len(og), len(ng)))
    elif canon(ng[:len(og)]) != canon(og):
        bad.append("기존 hooks.%s 그룹이 바뀌었다" % EVENT)
    elif canon(ng[-1]) != canon(group):
        bad.append("덧붙인 그룹이 조각과 다르다")
    return bad


def verify_remove(orig, new, cmd, script=None):
    bad = []
    if canon(new) != canon(remove(orig, cmd, script)):
        bad.append("뺀 결과가 'pwiki 항목만 뺀 것'과 다르다")
    if find_ours(new, cmd, script):
        bad.append("pwiki 항목이 남았다")
    oh = orig.get("hooks") or {}
    nh = new.get("hooks") or {}
    for ev in oh:
        if ev != EVENT and canon(oh[ev]) != canon(nh.get(ev)):
            bad.append("hooks.%s 가 바뀌었다" % ev)
    for k in orig:
        if k != "hooks" and canon(orig[k]) != canon(new.get(k)):
            bad.append("최상위 %s 가 바뀌었다" % k)
    return bad


def detect_format(obj, text):
    """원본 글과 같은 직렬화 방식을 찾는다. (indent, ensure_ascii, 끝 줄바꿈, 원본 그대로 재현 여부)"""
    nl = text.endswith("\n")
    for indent in (2, 4, "\t", 1, 3, None):
        for ea in (False, True):
            s = json.dumps(obj, indent=indent, ensure_ascii=ea)
            if s + ("\n" if nl else "") == text:
                return indent, ea, nl, True
    indent = 2
    for ln in text.splitlines()[1:3]:
        n = len(ln) - len(ln.lstrip(" "))
        if ln.startswith("\t"):
            indent = "\t"
            break
        if n:
            indent = n
            break
    return indent, False, nl, False


def dump(obj, fmt):
    indent, ea, nl, _ = fmt
    return json.dumps(obj, indent=indent, ensure_ascii=ea) + ("\n" if nl else "")


def summarize(obj):
    hooks = obj.get("hooks") or {}
    n_hooks = sum(len(g.get("hooks") or []) for gs in hooks.values() for g in gs)
    return "최상위 키 %d · hooks 이벤트 %d종(%s) · %s 그룹 %d · 훅 총 %d" % (
        len(obj), len(hooks), ", ".join(hooks.keys()) or "-", EVENT, len(hooks.get(EVENT) or []), n_hooks)


def write_atomic(target, data, orig_bytes, mode):
    d = os.path.dirname(target)
    tmp = os.path.join(d, ".%s.pwiki-merge-%d.tmp" % (os.path.basename(target), os.getpid()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, mode)
        with open(target, "rb") as fh:
            if fh.read() != orig_bytes:
                return False
        os.replace(tmp, target)
        return True
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main(argv=None):
    ap = argparse.ArgumentParser(description="settings.json 에 pwiki SessionStart 훅을 덧붙이거나 뺀다")
    ap.add_argument("--settings", default=os.path.join(os.path.expanduser("~"), ".claude", "settings.json"))
    ap.add_argument("--snippet", help="조각 JSON 파일(옛 방식)")
    ap.add_argument("--command", help="훅 명령(조각을 이것으로 만든다)")
    ap.add_argument("--python", help="--command 가 없을 때 명령을 만들 python 경로")
    ap.add_argument("--pwiki-home", help="--command 가 없을 때 명령에 붙일 PWIKI_HOME")
    ap.add_argument("--print-snippet", action="store_true", help="만든 조각을 출력하고 끝낸다")
    ap.add_argument("--apply", action="store_true", help="실제로 쓴다(없으면 미리보기)")
    ap.add_argument("--remove", action="store_true", help="덧붙인 pwiki 항목만 뺀다(되돌리기)")
    ap.add_argument("--script", help="--remove 에서만: 이 훅 파일 경로를 인자로 가진 명령도 뺀다")
    a = ap.parse_args(argv)
    if a.script and not a.remove:
        print("--script 는 --remove 와 함께만 쓴다")
        return 3
    if a.snippet and (a.command or a.python or a.pwiki_home):
        print("--snippet 과 --command·--python·--pwiki-home 은 함께 쓰지 않는다")
        return 3
    command = None if a.snippet else (a.command or make_command(a.python, a.pwiki_home))
    if a.print_snippet:
        if command is None:
            print("--print-snippet 은 --command(또는 기본 명령)로만 쓴다")
            return 3
        print(json.dumps(make_snippet(command), ensure_ascii=False, indent=2))
        return 0
    mode_name = ("빼기" if a.remove else "덧붙이기") + (" 적용" if a.apply else " 미리보기")
    p = print
    p("# pwiki settings %s" % mode_name)
    target = os.path.realpath(a.settings)
    p("대상: %s%s" % (a.settings, (" → 실제 파일 %s" % target) if target != os.path.abspath(a.settings) else ""))
    # 1) 합치기 전 검증
    try:
        group, cmd = load_snippet(a.snippet, command, check_files=not a.remove)
        if not os.path.isfile(target):
            raise Invalid("대상 파일이 없다")
        with open(target, "rb") as fh:
            orig_bytes = fh.read()
        try:
            text = orig_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise Invalid("대상이 UTF-8 이 아니다")
        orig = parse(text, "대상")
        check_hooks_shape(orig, "대상")
    except (Invalid, OSError) as e:
        p("합치기 전 검증: 실패 · %s" % (e if isinstance(e, Invalid) else type(e).__name__))
        p("판정: 실패 · 쓰지 않음")
        return 3
    st = os.stat(target)
    mode = stat.S_IMODE(st.st_mode)
    fmt = detect_format(orig, text)
    p("합치기 전 검증: 통과 · JSON 객체 · 중복 키 0 · hooks 구조 맞음 · 조각 명령의 파일 있음")
    p("전: %s" % summarize(orig))
    p("pwiki 항목: matcher %s · command %s · timeout %s" % (group.get("matcher"), cmd, group["hooks"][0].get("timeout")))
    if a.script:
        p("함께 뺄 명령: 인자에 %s 가 든 명령" % a.script)
    ours = find_ours(orig, cmd, a.script)
    # 2) 메모리에서 합치고 검사
    if a.remove:
        if not ours:
            others = other_pwiki_hooks(orig, cmd)
            p("pwiki 항목이 없다 · 바꿀 것 없음%s" % (
                (" · 다른 자리의 pwiki 훅 %d개는 그대로 둔다" % others) if others else ""))
            p("판정: 통과 · 쓰지 않음")
            return 0
        new = remove(orig, cmd, a.script)
        bad = verify_remove(orig, new, cmd, a.script)
    else:
        others = other_pwiki_hooks(orig, cmd)
        if others and not ours:
            p("합치기 전 검증: 실패 · 다른 명령의 pwiki 훅이 이미 %d개 있다(다른 설치 위치). 그 설치를 먼저 지운다" % others)
            p("판정: 실패 · 쓰지 않음")
            return 3
        if ours:
            same = any(canon(((orig.get("hooks") or {}).get(EVENT))[gi]) == canon(group) for gi, _ in ours)
            p("pwiki 항목이 이미 있다(%s) · 바꿀 것 없음" % ("조각과 같음" if same else "조각과 모양이 다름, 손대지 않음"))
            p("판정: 통과 · 쓰지 않음")
            return 0
        new = add(orig, group)
        bad = verify_add(orig, new, group)
    out_text = dump(new, fmt)
    try:
        if canon(parse(out_text, "합친 결과")) != canon(new):
            bad.append("직렬화한 글을 다시 읽으면 다른 객체다")
    except Invalid as e:
        bad.append("합친 결과가 JSON 이 아니다: %s" % e)
    p("후: %s" % summarize(new))
    p("원본 형식 재현: %s" % ("예(공백·이스케이프 그대로, 바뀌는 줄은 pwiki 항목뿐)" if fmt[3]
                          else "아니오(의미는 같고 공백·이스케이프만 다시 씀)"))
    if bad:
        p("합친 뒤 검사: 실패 · " + "; ".join(bad))
        p("판정: 실패 · 쓰지 않음")
        return 3
    p("합친 뒤 검사: 통과 · JSON 유효 · 기존 항목(키 순서 포함) 그대로 · %s" % (
        "pwiki 그룹만 뺌" if a.remove else "%s 끝에 그룹 하나만 늘어남" % EVENT))
    if not a.apply:
        p("판정: 통과 · 쓰지 않음(미리보기). 적용하려면 --apply")
        return 0
    # 3) 쓰기
    if not write_atomic(target, out_text.encode("utf-8"), orig_bytes, mode):
        p("쓰기: 중단 · 읽은 뒤 대상이 바뀌었다(다른 프로그램이 썼다). 다시 실행한다")
        return 4
    # 4) 쓴 뒤 검사
    with open(target, "rb") as fh:
        disk = fh.read()
    try:
        got = parse(disk.decode("utf-8"), "쓴 파일")
        bad = verify_remove(orig, got, cmd, a.script) if a.remove else verify_add(orig, got, group)
        if canon(got) != canon(new):
            bad.append("디스크 내용이 합친 결과와 다르다")
    except (Invalid, UnicodeDecodeError) as e:
        bad = ["쓴 파일을 다시 읽지 못했다: %s" % type(e).__name__]
    if bad or stat.S_IMODE(os.stat(target).st_mode) != mode:
        write_atomic(target, orig_bytes, disk, mode)
        p("쓴 뒤 검사: 실패 · %s · 원래 바이트로 되돌림" % "; ".join(bad or ["권한이 바뀌었다"]))
        return 5
    p("쓴 뒤 검사: 통과 · 디스크에서 다시 읽어 같은 검사를 했다 · 권한 %o 그대로" % mode)
    p("판정: 통과 · 썼음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
