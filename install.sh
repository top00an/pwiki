#!/bin/bash
# pwiki 설치기. 저장소를 푼 자리에서 그대로 돈다(파일을 다른 곳으로 복사하지 않는다).
#
# 단계: 요건 검사 → PWIKI_HOME 만들기(700) → 첫 수집(ingest --all) → vault 내보내기(export)
#       → (선택) 자동 수집 → (선택) 이어 하기 훅 → (선택) Claude Code 스킬(/pwiki, 말로 부르기)
# 다시 돌려도 안전하다: 수집은 이어 읽기, plist 는 같으면 두고, 훅은 이미 있으면 두지 않는다.
#
# 옵션:
#   --collector / --no-collector   자동 수집(맥: launchd 30분, 리눅스: crontab 안내만)
#   --hook / --no-hook             Claude Code SessionStart 이어 하기 훅(settings.json 에 덧붙이기)
#   --skill / --no-skill           Claude Code 스킬(settings.json 옆 skills/pwiki/SKILL.md). 세션 안에서 /pwiki 나 말로 부른다
#   --yes, -y                      정하지 않은 선택 단계를 모두 '예'로
#   --dry-run                      바꾸는 명령만 출력하고 바꾸지 않는다
#   --python PATH                  쓸 python3(기본: 맥은 /usr/bin/python3 먼저, 그다음 PATH)
#   --settings PATH                settings.json 위치(기본 ${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json)
# 환경변수: PWIKI_HOME(기본 ~/.pwiki), PWIKI_VAULT(기본 ~/pwiki),
#           PWIKI_CLAUDE_DIR(기본 ${CLAUDE_CONFIG_DIR:-~/.claude}), PWIKI_LAUNCHCTL(기본 /bin/launchctl)
# 종료 코드: 0 통과, 2 요건 미달·옵션 오류, 그 밖은 실패한 단계의 코드.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LABEL="local.pwiki.collect"

DRY=0; YES=0; COLLECTOR=""; HOOK=""; SKILL=""; PY_OPT=""; SETTINGS_OPT=""

# 도움말: 맨 위 주석 덩어리만(첫 코드 줄 앞에서 멈춘다)
usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --yes|-y) YES=1 ;;
    --collector) COLLECTOR=1 ;;
    --no-collector) COLLECTOR=0 ;;
    --hook) HOOK=1 ;;
    --no-hook) HOOK=0 ;;
    --skill) SKILL=1 ;;
    --no-skill) SKILL=0 ;;
    --python) [ $# -ge 2 ] || { echo "--python 에 경로가 없다" >&2; exit 2; }; PY_OPT="$2"; shift ;;
    --settings) [ $# -ge 2 ] || { echo "--settings 에 경로가 없다" >&2; exit 2; }; SETTINGS_OPT="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "모르는 옵션: $1 (--help)" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }
step() { printf '\n## %s\n' "$*"; }
# 셸에 그대로 붙여 넣을 수 있게 감싼다. 안전한 글자만이면 그대로, 아니면 작은따옴표로(한글 경로도 그대로 보인다)
SAFE_CHARS='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@%+=:,./-'
q() {
  case "$1" in
    ''|*[!"$SAFE_CHARS"]*) printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
    *) printf '%s' "$1" ;;
  esac
}
# 바꾸는 명령: --dry-run 이면 출력만, 아니면 출력하고 실행
run() {
  local s="+" a
  for a in "$@"; do s="$s $(q "$a")"; done
  say "$s"
  [ "$DRY" = 1 ] && return 0
  "$@"
}

abspath_or_die() {
  case "$2" in
    /*) ;;
    *) echo "$1 는 절대 경로여야 한다" >&2; exit 2 ;;
  esac
}

[ -n "${HOME:-}" ] || { echo "HOME 이 비었다" >&2; exit 2; }
OS="$(uname -s)"
CLAUDE_DIR="${PWIKI_CLAUDE_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}"
PWIKI_HOME="${PWIKI_HOME:-$HOME/.pwiki}"
PWIKI_VAULT="${PWIKI_VAULT:-$HOME/pwiki}"
SETTINGS="${SETTINGS_OPT:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json}"
LAUNCHCTL="${PWIKI_LAUNCHCTL:-/bin/launchctl}"
for pair in "PWIKI_CLAUDE_DIR:$CLAUDE_DIR" "PWIKI_HOME:$PWIKI_HOME" "PWIKI_VAULT:$PWIKI_VAULT" "settings:$SETTINGS"; do
  abspath_or_die "${pair%%:*}" "${pair#*:}"
done
export PWIKI_CLAUDE_DIR="$CLAUDE_DIR" PWIKI_HOME PWIKI_VAULT

say "# pwiki 설치$([ "$DRY" = 1 ] && echo ' (--dry-run: 바꾸는 명령만 출력, 바꾸지 않음)')"
say "저장소 $REPO"

# ---- 1. 요건 ------------------------------------------------------------------
step "1. 요건"
PROBE='
import sys
if sys.version_info < (3, 9):
    print("python %d.%d (3.9 이상 필요)" % sys.version_info[:2]); sys.exit(2)
import sqlite3
c = sqlite3.connect(":memory:")
try:
    c.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize=\"trigram\")")
except Exception:
    if sqlite3.sqlite_version_info < (3, 34, 0):
        why = "3.34 이상 필요"
    else:
        try:
            c.execute("CREATE VIRTUAL TABLE t2 USING fts5(x)")
            why = "FTS5 는 있으나 trigram 토크나이저가 없다"
        except Exception:
            why = "FTS5 없이 빌드된 SQLite 다"
    print("SQLite %s 에 FTS5 trigram 이 없다(%s)" % (sqlite3.sqlite_version, why)); sys.exit(3)
print("python %d.%d.%d · SQLite %s · FTS5 trigram 있음" % (sys.version_info[:3] + (sqlite3.sqlite_version,)))
'
candidates() {
  if [ -n "$PY_OPT" ]; then echo "$PY_OPT"; return; fi
  if [ "$OS" = Darwin ]; then
    # 명령줄 도구가 없으면 /usr/bin/python3 는 설치 창을 띄우는 껍데기라 건너뛴다(추측)
    if [ -x /usr/bin/python3 ] && /usr/bin/xcode-select -p >/dev/null 2>&1; then echo /usr/bin/python3; fi
    command -v python3 2>/dev/null || true
  else
    command -v python3 2>/dev/null || true
    [ -x /usr/bin/python3 ] && echo /usr/bin/python3
  fi
  return 0
}
PY=""
REASONS=""
while IFS= read -r c; do
  [ -n "$c" ] || continue
  if out="$("$c" -c "$PROBE" 2>&1)"; then PY="$c"; say "python: $c · $out"; break; fi
  REASONS="$REASONS
- $c: $(printf '%s' "$out" | tail -1)"
done <<EOC
$(candidates | awk '!seen[$0]++')
EOC
if [ -z "$PY" ]; then
  say "멈춤: 쓸 수 있는 python3 이 없다.${REASONS:-
- python3 을 찾지 못했다}"
  say "python 3.9 이상 + SQLite 3.34 이상(FTS5 trigram)이 필요하다. --python 으로 직접 줄 수 있다."
  exit 2
fi
case "$PY" in
  /*) ;;
  */*) PY="$(cd "$(dirname "$PY")" && pwd -P)/$(basename "$PY")" ;;
  *) PY="$(command -v "$PY" || true)" ;;  # 빗금 없는 이름은 PATH 에서 찾은 자리
esac
case "$PY" in /*) ;; *) say "멈춤: python 의 절대 경로를 정하지 못했다. --python 에 절대 경로를 준다"; exit 2 ;; esac
if [ ! -d "$CLAUDE_DIR/projects" ]; then
  say "멈춤: Claude Code 기록 폴더가 없다: $CLAUDE_DIR/projects"
  say "Claude Code 를 한 번 이상 쓴 계정에서 돌린다. 다른 곳이면 PWIKI_CLAUDE_DIR 로 준다."
  exit 2
fi
# 데이터 자리가 저장소·기록 폴더와 겹치거나, PWIKI_HOME·vault 자리에 pwiki 가 아닌 항목이 있으면 멈춘다
# (제거기의 --purge-data 가 저장소나 사람 파일을 지우지 않게, 데이터가 git 작업 트리나 사람 폴더에 섞이지 않게).
# pwiki 자리인지는 표시 파일(.pwiki-home·.pwiki-vault)로 가린다. 판정 규칙은 install/pwiki_data.py 에 있다.
OVERLAP='
import os, sys
repo, claude, home, vault = [os.path.realpath(x) for x in sys.argv[1:5]]
def inside(a, b):
    return a == b or a.startswith(b.rstrip("/") + "/")
for name, p in (("PWIKI_HOME", home), ("PWIKI_VAULT", vault)):
    if inside(p, repo) or inside(repo, p):
        print("%s(%s)가 pwiki 저장소(%s)와 겹친다. 저장소를 다른 폴더(예: ~/src/pwiki)에 두거나 %s 를 다른 곳으로 준다" % (name, p, repo, name))
        sys.exit(1)
    if inside(p, claude) or inside(claude, p):
        print("%s(%s)가 Claude Code 기록 폴더(%s)와 겹친다" % (name, p, claude))
        sys.exit(1)
if inside(home, vault) or inside(vault, home):
    print("PWIKI_HOME(%s)과 PWIKI_VAULT(%s)가 겹친다" % (home, vault))
    sys.exit(1)
'
if ! why="$("$PY" -c "$OVERLAP" "$REPO" "$CLAUDE_DIR" "$PWIKI_HOME" "$PWIKI_VAULT")"; then
  say "멈춤: $why"
  exit 2
fi
DATA=("$PY" "$REPO/install/pwiki_data.py")
if ! why="$("${DATA[@]}" check-install "$PWIKI_HOME" "$PWIKI_VAULT")"; then
  say "멈춤: $why"
  exit 2
fi
if [ -n "$why" ]; then say "$why"; fi
# 표시 파일 만들기(있으면 둔다). 제거기의 --purge-data 는 표시가 있는 자리만 pwiki 자리로 본다
mark() {
  [ -e "$1/$2" ] && return 0
  say "+ (만들기) $1/$2 (pwiki 폴더 표시)"
  [ "$DRY" = 1 ] || ( umask 077; printf '%s\n' "pwiki 가 쓰는 폴더 표시. install.sh 가 만들고 uninstall.sh --purge-data 가 지운다." > "$1/$2" )
}
NPROJ="$(find "$CLAUDE_DIR/projects" -mindepth 1 -maxdepth 1 -type d | wc -l | tr -d ' ')"
say "Claude Code 기록: $CLAUDE_DIR (프로젝트 폴더 ${NPROJ}개)"
say "PWIKI_HOME $PWIKI_HOME · vault $PWIKI_VAULT"

# ---- 2. PWIKI_HOME ---------------------------------------------------------------
step "2. 데이터 폴더(권한 700)"
for d in "$PWIKI_HOME" "$PWIKI_HOME/logs" "$PWIKI_HOME/runs"; do
  if [ ! -d "$d" ]; then run mkdir -p -m 700 "$d"; fi
done
if [ -d "$PWIKI_HOME" ] && [ "$("$PY" -c 'import os,sys; print("%o" % (os.stat(sys.argv[1]).st_mode & 0o777))' "$PWIKI_HOME")" != 700 ]; then
  run chmod 700 "$PWIKI_HOME"
fi
mark "$PWIKI_HOME" .pwiki-home
SECRETS="$PWIKI_HOME/secrets.local"
if [ ! -e "$SECRETS" ]; then
  say "+ (만들기) $SECRETS (권한 600, 주석만)"
  if [ "$DRY" = 0 ]; then
    ( umask 077; cat > "$SECRETS" <<'EOS'
# pwiki 알려진 비밀값 목록. 한 줄에 값 하나, '#' 로 시작하면 주석, 6자 미만은 무시한다.
# 여기 적은 값은 적재·검색·vault·이어 하기에서 [REDACTED:known] 으로 가려진다.
# 값을 적었으면 'pwiki rederive --apply' 로 이미 쌓인 기록에도 다시 적용한다.
# 이 파일은 권한 600 으로 두고, 복사하거나 공유하지 않는다.
EOS
    )
  fi
else
  say "secrets.local 있음(그대로 둔다)"
fi

# ---- 3. 첫 수집 ----------------------------------------------------------------
PW=("$PY" "$REPO/pwiki")
step "3. 수집(ingest --all, 이어 읽기)"
if [ "$DRY" = 1 ]; then
  run "${PW[@]}" ingest --all
else
  say "+ $(q "$PY") $(q "$REPO/pwiki") ingest --all"
  OUT="$PWIKI_HOME/logs/install.ingest.txt"
  set +e
  ( umask 077; "${PW[@]}" ingest --all > "$OUT" 2>&1 )
  rc=$?
  set -e
  sed -n '1,7p' "$OUT"
  say "(전체 출력: $OUT)"
  case "$rc" in
    0) ;;
    2) say "경고: 줄 수 대조가 맞지 않는 파일이 있다(pwiki verify 로 본다). 계속한다." ;;
    75) say "경고: 다른 수집이 돌고 있다. 그 수집이 끝나면 반영된다. 계속한다." ;;
    *) say "멈춤: ingest 실패(rc $rc)"; exit "$rc" ;;
  esac
fi

# ---- 4. 내보내기 ----------------------------------------------------------------
step "4. vault 내보내기(export)"
[ -d "$PWIKI_VAULT" ] || run mkdir -p "$PWIKI_VAULT"
mark "$PWIKI_VAULT" .pwiki-vault
if [ "$DRY" = 1 ]; then
  run "${PW[@]}" export
else
  say "+ $(q "$PY") $(q "$REPO/pwiki") export"
  set +e
  "${PW[@]}" export
  rc=$?
  set -e
  case "$rc" in
    0) ;;
    4) say "경고: 사람이 고친 페이지(충돌) 또는 가림 검사로 막은 페이지가 있다. 덮지 않았다. 계속한다." ;;
    *) say "멈춤: export 실패(rc $rc)"; exit "$rc" ;;
  esac
fi

# ---- 선택 단계 ----------------------------------------------------------------
ask() {  # ask <변수값> <질문>  → 0 이면 예
  local v="$1"
  if [ "$v" = 1 ]; then return 0; fi
  if [ "$v" = 0 ]; then return 1; fi
  if [ "$YES" = 1 ]; then return 0; fi
  if [ -t 0 ]; then
    local ans=""
    printf '%s [y/N] ' "$2"
    read -r ans || ans=""
    case "$ans" in y|Y|yes|YES) return 0 ;; esac
    return 1
  fi
  say "(대화형이 아니라 건너뜀. 켜려면 다시 돌릴 때 옵션을 준다)"
  return 1
}

COLLECTOR_ON=0
HOOK_ON=0
HOOK_CMD=""
PLIST=""
SKILL_ON=0
SKILL_DIR="$(dirname "$SETTINGS")/skills/pwiki"

step "5. 자동 수집(30분마다 ingest --all)"
if ask "$COLLECTOR" "자동 수집을 켤까요?"; then
  COLLECTOR_ON=1
  if [ "$OS" = Darwin ]; then
    AGENTS="$HOME/Library/LaunchAgents"
    PLIST="$AGENTS/$LABEL.plist"
    TMP_PLIST="$(mktemp "${TMPDIR:-/tmp}/pwiki-plist.XXXXXX")"
    trap 'rm -f "$TMP_PLIST"' EXIT
    "$PY" "$REPO/install/render_plist.py" --python "$PY" --repo "$REPO" --pwiki-home "$PWIKI_HOME" \
      --claude-dir "$CLAUDE_DIR" --vault "$PWIKI_VAULT" --out "$TMP_PLIST"
    if [ -x /usr/bin/plutil ]; then /usr/bin/plutil -lint "$TMP_PLIST" >/dev/null; fi
    DOMAIN="gui/$(id -u)"
    LOADED=0
    if [ -f "$PLIST" ] && [ "$DRY" = 0 ] && "$LAUNCHCTL" print "$DOMAIN/$LABEL" >/dev/null 2>&1; then LOADED=1; fi
    if [ -f "$PLIST" ] && cmp -s "$TMP_PLIST" "$PLIST"; then
      say "plist 같음: $PLIST"
      if [ "$LOADED" = 1 ]; then say "launchd 에 이미 올라 있다(그대로 둔다)"; else run "$LAUNCHCTL" bootstrap "$DOMAIN" "$PLIST"; fi
    else
      if [ "$LOADED" = 1 ]; then run "$LAUNCHCTL" bootout "$DOMAIN/$LABEL"; fi
      [ -d "$AGENTS" ] || run mkdir -p "$AGENTS"
      run install -m 644 "$TMP_PLIST" "$PLIST"
      run "$LAUNCHCTL" bootstrap "$DOMAIN" "$PLIST"
    fi
    say "기록: $PWIKI_HOME/logs/collect.log · collect.status.json"
  else
    say "리눅스는 자동 등록하지 않는다. 'crontab -e' 로 아래 한 줄을 넣는다:"
    say "*/30 * * * * PWIKI_HOME=$(q "$PWIKI_HOME") PWIKI_CLAUDE_DIR=$(q "$CLAUDE_DIR") PWIKI_VAULT=$(q "$PWIKI_VAULT") $(q "$PY") $(q "$REPO/install/pwiki_collect.py") >/dev/null 2>&1"
  fi
else
  say "자동 수집: 건너뜀"
fi

step "6. 이어 하기 훅(Claude Code SessionStart)"
if ask "$HOOK" "새 세션을 열 때 이어 하기 요약을 붙일까요?"; then
  HOOK_ON=1
  MERGE=("$PY" "$REPO/install/merge_settings.py" --settings "$SETTINGS" --python "$PY" --pwiki-home "$PWIKI_HOME")
  HOOK_CMD="$("${MERGE[@]}" --print-snippet | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["hooks"]["SessionStart"][0]["hooks"][0]["command"])')"
  say "훅 명령: $HOOK_CMD"
  if [ ! -e "$SETTINGS" ]; then
    run mkdir -p "$(dirname "$SETTINGS")"
    say "+ (만들기) $SETTINGS 내용 {} (권한 600)"
    [ "$DRY" = 1 ] || ( umask 077; printf '{}\n' > "$SETTINGS" )
  fi
  if [ "$DRY" = 1 ]; then
    if [ -e "$SETTINGS" ]; then "${MERGE[@]}" | sed 's/^/  | /'; fi
    run "${MERGE[@]}" --apply
  else
    say "+ $(q "$PY") $(q "$REPO/install/merge_settings.py") --settings $(q "$SETTINGS") … --apply"
    set +e
    "${MERGE[@]}" --apply | sed 's/^/  | /'
    rc=${PIPESTATUS[0]}
    set -e
    [ "$rc" = 0 ] || { say "멈춤: settings 덧붙이기 실패(rc $rc). settings.json 은 바꾸지 않았거나 원래 바이트로 되돌렸다."; exit "$rc"; }
  fi
else
  say "이어 하기 훅: 건너뜀"
fi

step "7. Claude Code 스킬(/pwiki, 말로 부르기)"
if ask "$SKILL" "Claude Code 세션 안에서 pwiki 를 쓰게 스킬을 둘까요?"; then
  SKILL_ARGS=("$PY" "$REPO/install/skill.py" install --dir "$SKILL_DIR" --repo "$REPO" --python "$PY")
  [ "$DRY" = 1 ] && SKILL_ARGS+=(--dry-run)
  set +e
  "${SKILL_ARGS[@]}" | sed 's/^/  | /'
  rc=${PIPESTATUS[0]}
  set -e
  if [ "$rc" = 0 ]; then
    SKILL_ON=1
    say "세션 안에서: /pwiki, /pwiki 어제, /pwiki 검색어 · 또는 '어제 뭐 했지?'처럼 말로"
  else
    say "스킬: 건너뜀(위 이유). 나머지 설치는 그대로다"
  fi
else
  say "스킬: 건너뜀"
fi

# ---- 설치 기록(제거기가 읽는다) -----------------------------------------------------------
STATE="$PWIKI_HOME/install.json"
if [ "$DRY" = 0 ]; then
  ( umask 077; "$PY" - "$STATE" "$REPO" "$PY" "$COLLECTOR_ON" "$PLIST" "$HOOK_ON" "$HOOK_CMD" "$SETTINGS" "$PWIKI_VAULT" "$SKILL_ON" "$SKILL_DIR" <<'EOP'
import json, os, sys
path, repo, py, col, plist, hook, cmd, settings, vault, skill, skill_dir = sys.argv[1:]
old = {}
try:
    with open(path, encoding="utf-8") as fh:
        old = json.load(fh)
except (OSError, ValueError):
    pass
st = {"repo": repo, "python": py, "vault": vault,
      "collector": bool(int(col)) or bool(old.get("collector")), "plist": plist or old.get("plist") or "",
      "hook": bool(int(hook)) or bool(old.get("hook")), "hook_command": cmd or old.get("hook_command") or "",
      "settings": settings if int(hook) else (old.get("settings") or settings),
      "skill": bool(int(skill)) or bool(old.get("skill")),
      "skill_dir": skill_dir if int(skill) else (old.get("skill_dir") or "")}
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(st, fh, ensure_ascii=False, indent=1)
    fh.write("\n")
os.replace(tmp, path)
EOP
  )
fi

step "끝"
say "명령 예: $(q "$PY") $(q "$REPO/pwiki") today"
say "줄여 쓰려면 셸 설정에: alias pwiki=$(q "$(q "$PY") $(q "$REPO/pwiki")")"
say "제거: $(q "$REPO/uninstall.sh") (데이터는 남긴다. 지우려면 --purge-data)"
exit 0
