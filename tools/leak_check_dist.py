#!/usr/bin/python3
"""배포본 누출 검사. 값은 출력하지 않는다(종류·자리·개수만).

(가) 금지 목록(--denylist, 저장소 밖 파일. 한 줄에 낱말 하나, # 주석): 파일 내용·파일 이름·커밋 메시지·작성자 이름.
     대소문자 무시 부분 일치. 걸린 낱말은 목록의 줄 번호(#n)로만 알린다.
(나) 이 컴퓨터 사용자의 홈 경로·사용자 이름·전체 이름(실행할 때 읽는다. 저장소에 적지 않는다).
(다) pwiki 알려진 값(secrets.local)·수확값(secrets.harvested): pwikilib.leakscan 대조기와 가림기의 known 계열로 센다.
     값은 메모리에만 두고 개수만 낸다. PWIKI_HOME 으로 어느 목록을 읽을지 정한다.
     알려진 값은 어디서 걸려도 실패다. 수확값은 pwiki 를 Claude Code 로 만들며 시험 파일의 가짜 값까지 수확했을 수 있어,
     tests/ 안에서만 걸린 수확값은 '시험 가짜값 추정'으로 따로 세고 실패로 보지 않는다(--strict 면 실패. 사람이 확인한다).
(라) 이메일(예시 도메인 제외)·사설 IP(10.0.0.0/24 예시 대역 제외)·토큰 모양(가짜로 보이는 모양 제외).
(마) git 저장소면: 커밋 수(--single-commit 이면 1 이어야 한다), 작성자·커밋한 사람 이메일(--expect-email).
(바) 이 컴퓨터의 Claude Code 프로젝트 폴더 이름(<기록 폴더>/projects, 실행할 때 읽는다): 홈·임시 폴더 앞부분과
     번호·uuid 꼬리를 뗀 나머지가 12자 이상이면 대소문자 무시 부분 일치로 찾는다. 걸린 이름은 #n 으로만 알린다.

사용: leak_check_dist.py <폴더> [--denylist PATH] [--expect-email ADDR] [--expect-name NAME] [--single-commit]
종료 코드: 0 깨끗, 1 걸림 또는 판정 못 함, 2 사용 오류.
"""
import argparse
import base64
import getpass
import ipaddress
import os
import re
import subprocess
import sys

SKIP_DIRS = {".git", "__pycache__"}
GENERIC_USERS = {"root", "admin", "user", "users", "ubuntu", "runner", "tester", "test", "debian", "ec2-user", "home"}

EMAIL_RX = re.compile(r"[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})")
IP_RX = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])")
TOKEN_RX = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.(eyJ[A-Za-z0-9_-]{8,})\.[A-Za-z0-9_-]{8,}")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----([\s\S]{0,8000}?)-----END [A-Z ]*PRIVATE KEY-----")),
]
# 사설 대역(RFC 1918)과 예시로 허용하는 대역. 점 표기 리터럴을 쓰지 않는다(이 파일 자신이 걸리지 않게).
PRIVATE_NETS = [ipaddress.ip_network((a << 24 | b << 16, n)) for a, b, n in ((10, 0, 8), (172, 16, 12), (192, 168, 16))]
EXAMPLE_NET = ipaddress.ip_network((10 << 24, 24))


_FAMILY_TAIL = re.compile(r"(?:-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?-\d{3,}$")
_TMP_HEADS = ("-private-var-folders-", "-private-tmp-", "-var-folders-", "-tmp-")


def project_marks(claude_dir=None, home=None):
    """(바) 프로젝트 폴더 이름에서 사적일 수 있는 조각을 뽑는다. 값은 메모리에만 둔다."""
    cdir = claude_dir or os.environ.get("PWIKI_CLAUDE_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    pdir = os.path.join(cdir, "projects")
    try:
        names = sorted(n for n in os.listdir(pdir) if os.path.isdir(os.path.join(pdir, n)))
    except OSError:
        return []
    hp = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(home or os.path.expanduser("~")))
    out = []
    for n in names:
        frag = n
        if frag.startswith(hp + "-"):
            frag = frag[len(hp) + 1:]
        else:
            for h in _TMP_HEADS:
                if frag.startswith(h):
                    frag = frag[len(h):]
                    break
        frag = _FAMILY_TAIL.sub("", frag).strip("-").lower()
        if len(frag) >= 12 and frag not in out:
            out.append(frag)
    return out


def walk(root):
    for dp, dn, fn in os.walk(root):
        dn[:] = sorted(d for d in dn if d not in SKIP_DIRS)
        for f in sorted(fn):
            ap = os.path.join(dp, f)
            if os.path.isfile(ap) and not os.path.islink(ap):
                yield os.path.relpath(ap, root), ap


def read_text(ap):
    with open(ap, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def line_of(text, pos):
    return text.count("\n", 0, pos) + 1


def load_denylist(path):
    words = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            w = ln.strip()
            if w and not w.startswith("#"):
                words.append(w)
    return words


def user_marks(home=None):
    out = []
    home = (home or os.path.expanduser("~")).rstrip("/")
    if len(home) > 1:
        out.append(("홈 경로", re.compile(re.escape(home))))
    names = set()
    try:
        names.add(getpass.getuser())
    except Exception:
        pass
    try:
        import pwd
        pw = pwd.getpwuid(os.getuid())
        names.add(pw.pw_name)
        full = (pw.pw_gecos or "").split(",")[0].strip()
        if len(full) >= 4:
            out.append(("전체 이름", re.compile(re.escape(full), re.I)))
    except Exception:
        pass
    for n in sorted(names):
        if n and len(n) >= 3 and n.lower() not in GENERIC_USERS:
            out.append(("사용자 이름", re.compile(r"(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])", re.I)))
    return out


def example_domain(dom):
    d = dom.lower()
    labels = d.split(".")
    return any(x.startswith("example") for x in labels) or d.endswith((".test", ".invalid", ".localhost")) \
        or d in ("localhost",)


def flagged_ip(s):
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_unspecified or ip.is_link_local or ip.is_multicast or ip in EXAMPLE_NET:
        return False
    return any(ip in n for n in PRIVATE_NETS)


_SEQS = ("abcdefghijklmnopqrstuvwxyz", "0123456789")


def looks_fake(tok):
    """시험용 가짜로 보이는 값: 알파벳·숫자 순서열 6자 이상, 같은 글자 5번 이상, fake·example 류 낱말."""
    low = tok.lower()
    if re.search(r"fake|example|dummy|sample|test|placeholder", low) or re.search(r"(.)\1{4,}", tok):
        return True
    for seq in _SEQS:
        for i in range(len(seq) - 5):
            if seq[i:i + 6] in low:
                return True
    return False


def token_fake(kind, m):
    if kind == "jwt":
        part = m.group(1)
        try:
            dec = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode("utf-8", "replace")
        except (ValueError, TypeError):
            dec = ""
        return looks_fake(dec) or looks_fake(m.group(0))
    if kind == "private_key":
        return len(re.sub(r"\s", "", m.group(1))) < 200
    return looks_fake(m.group(0))


def secret_counter(root):
    """(다) 알려진 값·수확값 대조기. (값 개수 정보, 세기 함수) 또는 (None, 이유)."""
    sys.path.insert(0, root)
    try:
        from pwikilib import leakscan, redact
    except Exception as e:  # 배포본에 pwikilib 가 없으면 판정 못 함
        return None, "pwikilib 를 읽지 못했다(%s)" % type(e).__name__
    warn = []
    known = redact.load_known_values(warn=warn)
    hv = redact.Harvested().load(warn=warn)
    strong = [v for v in hv.active_strong() if v not in known]
    vals = known + strong
    probes = leakscan.make_probes(vals)
    R = redact.Redactor(vals, weak_digests=hv.weak)

    Rk = redact.Redactor(known)
    kprobes = {v: probes[v] for v in known if v in probes}

    def count(ap, text):
        """(알려진 값 걸림, 수확값 걸림)."""
        n = sum(leakscan.count_in_file(ap, vals, probes).values()) if vals else 0
        nk = sum(leakscan.count_in_file(ap, known, kprobes).values()) if known else 0
        R.counts = {}
        R.text(text)
        n2 = sum(v for k, v in R.counts.items() if k.startswith("known"))
        Rk.counts = {}
        Rk.text(text)
        nk2 = sum(v for k, v in Rk.counts.items() if k.startswith("known"))
        nk = max(nk, nk2)
        return nk, max(n, n2) - nk if max(n, n2) > nk else 0
    return {"known": len(known), "strong": len(strong), "weak": len(hv.weak)}, count


def git(root, *args):
    p = subprocess.run(["git", "-C", root] + list(args), capture_output=True)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def main(argv=None):
    ap = argparse.ArgumentParser(description="배포본 누출 검사(값은 출력하지 않는다)")
    ap.add_argument("root")
    ap.add_argument("--denylist", help="금지 목록 파일(저장소 밖)")
    ap.add_argument("--expect-email")
    ap.add_argument("--expect-name")
    ap.add_argument("--single-commit", action="store_true")
    ap.add_argument("--home", help="(나) 에 쓸 홈 경로(기본 지금 사용자)")
    ap.add_argument("--strict", action="store_true", help="tests/ 안에서만 걸린 수확값도 실패로 본다")
    ap.add_argument("--claude-dir", help="(바) 에 쓸 Claude Code 기록 폴더(기본 PWIKI_CLAUDE_DIR 또는 ~/.claude)")
    a = ap.parse_args(argv)
    root = os.path.realpath(a.root)
    if not os.path.isdir(root):
        print("폴더가 없다")
        return 2
    words = []
    if a.denylist:
        try:
            words = load_denylist(a.denylist)
        except OSError:
            print("금지 목록을 읽지 못했다")
            return 2
    marks = user_marks(a.home)
    pmarks = project_marks(a.claude_dir, a.home)
    info, counter = secret_counter(root)
    hits = {"가": [], "나": [], "다": [], "라": [], "마": [], "바": []}
    fake_n = 0
    test_harv = []
    files = [(rel, p) for rel, p in walk(root) if rel != ".git"]  # 작업 트리의 .git 파일(gitdir 가리킴)은 뺀다
    for rel, apath in files:
        text = read_text(apath)
        low = text.lower()
        rlow = rel.lower()
        for i, w in enumerate(words, 1):
            wl = w.lower()
            if wl in rlow:
                hits["가"].append("낱말 #%d · 파일 이름 %s" % (i, rel))
            p = low.find(wl)
            while p >= 0:
                hits["가"].append("낱말 #%d · %s:%d" % (i, rel, line_of(text, p)))
                p = low.find(wl, p + 1)
        for i, w in enumerate(pmarks, 1):
            if w in rlow:
                hits["바"].append("프로젝트 이름 #%d · 파일 이름 %s" % (i, rel))
            p = low.find(w)
            while p >= 0:
                hits["바"].append("프로젝트 이름 #%d · %s:%d" % (i, rel, line_of(text, p)))
                p = low.find(w, p + 1)
        for kind, rx in marks:
            for m in rx.finditer(text):
                hits["나"].append("%s · %s:%d" % (kind, rel, line_of(text, m.start())))
            if rx.search(rel):
                hits["나"].append("%s · 파일 이름 %s" % (kind, rel))
        if counter is not None:
            nk, nh = counter(apath, text)
            if nk:
                hits["다"].append("알려진 값 %d건 · %s" % (nk, rel))
            if nh:
                if rel.startswith("tests/") and not a.strict:
                    test_harv.append("%d건 · %s" % (nh, rel))
                else:
                    hits["다"].append("수확값 %d건 · %s" % (nh, rel))
        for m in EMAIL_RX.finditer(text):
            if not example_domain(m.group(1)):
                hits["라"].append("이메일 · %s:%d" % (rel, line_of(text, m.start())))
        for m in IP_RX.finditer(text):
            if flagged_ip(m.group(1)):
                hits["라"].append("사설 IP · %s:%d" % (rel, line_of(text, m.start())))
        for kind, rx in TOKEN_RX:
            for m in rx.finditer(text):
                if token_fake(kind, m):
                    fake_n += 1
                else:
                    hits["라"].append("토큰 모양 %s · %s:%d" % (kind, rel, line_of(text, m.start())))
    git_line = "git 저장소 아님(건너뜀)"
    if os.path.isdir(os.path.join(root, ".git")):
        rc, n = git(root, "rev-list", "--count", "HEAD")
        rc2, meta = git(root, "log", "--format=%an%x00%ae%x00%cn%x00%ce")
        rc3, msgs = git(root, "log", "--format=%B")
        if rc or rc2 or rc3:
            hits["마"].append("git 정보를 읽지 못했다")
        else:
            n = int(n.strip() or 0)
            if a.single_commit and n != 1:
                hits["마"].append("커밋 %d개(1개여야 한다)" % n)
            for ln in meta.splitlines():
                an, ae, cn, ce = (ln.split("\x00") + ["", "", "", ""])[:4]
                if a.expect_email and (ae != a.expect_email or ce != a.expect_email):
                    hits["마"].append("작성자·커밋한 사람 이메일이 기대값과 다르다")
                if a.expect_name and (an != a.expect_name or cn != a.expect_name):
                    hits["마"].append("작성자·커밋한 사람 이름이 기대값과 다르다")
                for i, w in enumerate(words, 1):
                    if w.lower() in (an + "\n" + cn).lower():
                        hits["가"].append("낱말 #%d · 커밋 작성자 이름" % i)
            for i, w in enumerate(words, 1):
                if w.lower() in msgs.lower():
                    hits["가"].append("낱말 #%d · 커밋 메시지" % i)
            for i, w in enumerate(pmarks, 1):
                if w in msgs.lower() or w in meta.lower():
                    hits["바"].append("프로젝트 이름 #%d · 커밋 메시지·작성자" % i)
            git_line = "커밋 %d개" % n
    out = ["# 배포본 누출 검사(값은 출력하지 않는다)", "대상 파일 %d개 · %s" % (len(files), git_line)]
    out.append("(가) 금지 목록 %s: 걸림 %d건" % ("%d줄" % len(words) if a.denylist else "없음(건너뜀)", len(hits["가"])))
    out.append("(나) 사용자 흔적(홈 경로·이름 %d종): 걸림 %d건" % (len(marks), len(hits["나"])))
    if counter is None:
        out.append("(다) 판정 못 함: %s" % info)
        hits["다"].append("판정 못 함")
    else:
        out.append("(다) 알려진 값 %d개 · 수확값 %d개 · 약한 값 해시 %d묶음: 걸림 %d파일 · tests/ 안에서만 걸린 수확값"
                   "(시험 가짜값 추정, 사람 확인) %d파일 %d건" % (
                       info["known"], info["strong"], info["weak"], len(hits["다"]), len(test_harv),
                       sum(int(x.split("건")[0]) for x in test_harv)))
        for x in test_harv:
            out.append("- (다·참고) %s" % x)
    out.append("(라) 이메일·사설 IP·토큰 모양: 걸림 %d건 · 가짜 모양으로 본 토큰 %d건" % (len(hits["라"]), fake_n))
    out.append("(마) git: 걸림 %d건" % len(hits["마"]))
    out.append("(바) 이 컴퓨터의 Claude Code 프로젝트 이름 %d개: 걸림 %d건" % (len(pmarks), len(hits["바"])))
    bad = sum(len(v) for v in hits.values())
    for k in ("가", "나", "다", "라", "마", "바"):
        for h in hits[k][:30]:
            out.append("- (%s) %s" % (k, h))
        if len(hits[k]) > 30:
            out.append("- (%s) 외 %d건" % (k, len(hits[k]) - 30))
    out.append("판정: %s" % ("통과(0건)" if not bad else "실패(%d건)" % bad))
    print("\n".join(out))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
