#!/bin/bash
# pwiki 제거기. 이어 하기 훅과 자동 수집을 뺀다. 데이터(PWIKI_HOME, vault)는 기본으로 남긴다.
#
# 옵션:
#   --purge-data     pwiki 가 만든 데이터까지 지운다. 되돌릴 수 없다(아무것도 바꾸기 전에 확인을 묻는다).
#                    PWIKI_HOME 은 DB·secrets·잠금·설치 기록·pwiki 로그만, vault 는 export 기록의 페이지와
#                    안내·표시 파일만 지운다. 그 밖의 파일은 남기고 수를 알린다. 폴더는 비었을 때만 지운다.
#                    표시 파일(.pwiki-home·.pwiki-vault)도 export 기록도 없는 사람 폴더는 건드리지 않는다
#   --yes, -y        확인을 묻지 않는다(--purge-data 와 함께 쓰면 바로 지운다)
#   --dry-run        바꾸는 명령만 출력하고 바꾸지 않는다
#   --python PATH    merge_settings 를 돌릴 python3(기본: 설치 기록의 python, 없으면 /usr/bin/python3·PATH)
#   --settings PATH  settings.json 위치(기본: 설치 기록, 없으면 ${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json)
# 환경변수: PWIKI_HOME(기본 ~/.pwiki), PWIKI_VAULT(기본 ~/pwiki), PWIKI_LAUNCHCTL(기본 /bin/launchctl)
#   PWIKI_HOME 을 바꿔 설치했으면 같은 값을 준다(설치 기록 install.json 이 그 안에 있다).
# 훅은 설치 기록의 명령과, 이 저장소의 install/pwiki_session_start.py 를 부르는 명령을 뺀다(다른 자리의 pwiki 훅은 둔다).
# launchd 는 plist 가 이 저장소의 수집기를 가리킬 때만 내린다.
# 종료 코드: 0 통과, 2 옵션 오류·확인 거절, 그 밖은 실패한 단계의 코드.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
LABEL="local.pwiki.collect"
DRY=0; YES=0; PURGE=0; PY_OPT=""; SETTINGS_OPT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --purge-data) PURGE=1 ;;
    --yes|-y) YES=1 ;;
    --dry-run) DRY=1 ;;
    --python) [ $# -ge 2 ] || { echo "--python 에 경로가 없다" >&2; exit 2; }; PY_OPT="$2"; shift ;;
    --settings) [ $# -ge 2 ] || { echo "--settings 에 경로가 없다" >&2; exit 2; }; SETTINGS_OPT="$2"; shift ;;
    -h|--help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) echo "모르는 옵션: $1 (--help)" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '%s\n' "$*"; }
step() { printf '\n## %s\n' "$*"; }
# 셸에 그대로 붙여 넣을 수 있게 감싼다(install.sh 와 같은 규칙)
SAFE_CHARS='abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@%+=:,./-'
q() {
  case "$1" in
    ''|*[!"$SAFE_CHARS"]*) printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")" ;;
    *) printf '%s' "$1" ;;
  esac
}
run() {
  local s="+" a
  for a in "$@"; do s="$s $(q "$a")"; done
  say "$s"
  [ "$DRY" = 1 ] && return 0
  "$@"
}

[ -n "${HOME:-}" ] || { echo "HOME 이 비었다" >&2; exit 2; }
OS="$(uname -s)"
PWIKI_HOME="${PWIKI_HOME:-$HOME/.pwiki}"
CLAUDE_DIR="${PWIKI_CLAUDE_DIR:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}}"
LAUNCHCTL="${PWIKI_LAUNCHCTL:-/bin/launchctl}"
STATE="$PWIKI_HOME/install.json"
HOOK_SCRIPT="$REPO/install/pwiki_session_start.py"
COLLECT_SCRIPT="$REPO/install/pwiki_collect.py"

# 설치 기록에서 값 하나 읽기(없으면 빈 글)
PY_FOR_STATE=""
for c in "$PY_OPT" /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  if [ -n "$c" ] && [ -x "$c" ]; then PY_FOR_STATE="$c"; break; fi
done
[ -n "$PY_FOR_STATE" ] || { echo "python3 을 찾지 못했다(--python)" >&2; exit 2; }
state() {
  [ -f "$STATE" ] || return 0
  "$PY_FOR_STATE" -c 'import json,sys
try:
    v = json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2], "")
except Exception:
    v = ""
print("1" if v is True else ("" if v is False else v))' "$STATE" "$1"
}

PY="${PY_OPT:-$(state python)}"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then PY="$PY_FOR_STATE"; fi
PWIKI_VAULT="${PWIKI_VAULT:-$(state vault)}"
PWIKI_VAULT="${PWIKI_VAULT:-$HOME/pwiki}"
SETTINGS="${SETTINGS_OPT:-$(state settings)}"
SETTINGS="${SETTINGS:-${CLAUDE_CONFIG_DIR:-$HOME/.claude}/settings.json}"
HOOK_CMD="$(state hook_command)"

# 데이터 자리 판정과 지우기(pwiki 가 만든 것만): install/pwiki_data.py
DATA=("$PY" "$REPO/install/pwiki_data.py" purge "$PWIKI_HOME" "$PWIKI_VAULT" "$HOME" "$REPO" "$CLAUDE_DIR")

say "# pwiki 제거$([ "$DRY" = 1 ] && echo ' (--dry-run: 바꾸는 명령만 출력, 바꾸지 않음)')"
if [ -f "$STATE" ]; then
  say "설치 기록: $STATE"
else
  say "설치 기록: 없음($STATE). PWIKI_HOME 을 바꿔 설치했으면 같은 PWIKI_HOME 으로 다시 돌린다."
  say "훅은 이 저장소의 훅 파일을 부르는 명령을 찾아 뺀다."
fi

# ---- 0. 지울 데이터 확인(--purge-data): 아무것도 바꾸기 전에 묻는다 ----------------------------
DATA_DEL=0
if [ "$PURGE" = 1 ]; then
  step "0. 지울 데이터 확인"
  say "지울 것(되돌릴 수 없다. pwiki 가 만든 것만):"
  set +e
  "${DATA[@]}"
  rc=$?
  set -e
  case "$rc" in
    0) DATA_DEL=1 ;;
    3) ;;
    *) say "멈춤: 데이터 확인 실패(rc $rc). 아무것도 바꾸지 않았다"; exit "$rc" ;;
  esac
  if [ "$DATA_DEL" = 0 ]; then
    say "지울 데이터가 없다"
  elif [ "$YES" != 1 ]; then
    if [ -t 0 ]; then
      printf '정말 지울까요? 되돌릴 수 없다. 지우려면 delete 를 친다: '
      read -r ans || ans=""
      [ "$ans" = delete ] || { say "취소: 아무것도 바꾸지 않았다(훅·자동 수집·데이터 그대로)"; exit 2; }
    else
      say "멈춤: 대화형이 아니다. 아무것도 바꾸지 않았다. 지우려면 --purge-data --yes"; exit 2
    fi
  fi
fi

# ---- 1. 훅 ---------------------------------------------------------------------
step "1. 이어 하기 훅 빼기"
if [ -f "$SETTINGS" ]; then
  if [ -n "$HOOK_CMD" ]; then
    MERGE=("$PY" "$REPO/install/merge_settings.py" --settings "$SETTINGS" --command "$HOOK_CMD" --remove --script "$HOOK_SCRIPT")
  else
    MERGE=("$PY" "$REPO/install/merge_settings.py" --settings "$SETTINGS" --python "$PY" --pwiki-home "$PWIKI_HOME" --remove --script "$HOOK_SCRIPT")
  fi
  if [ "$DRY" = 1 ]; then
    "${MERGE[@]}" | sed 's/^/  | /'
    run "${MERGE[@]}" --apply
  else
    set +e
    "${MERGE[@]}" --apply | sed 's/^/  | /'
    rc=${PIPESTATUS[0]}
    set -e
    [ "$rc" = 0 ] || { say "멈춤: settings 빼기 실패(rc $rc). settings.json 은 바꾸지 않았거나 원래 바이트로 되돌렸다."; exit "$rc"; }
  fi
else
  say "settings.json 없음: $SETTINGS (건너뜀)"
fi

# ---- 2. 자동 수집 -------------------------------------------------------------------
step "2. 자동 수집 빼기"
# plist 가 이 저장소의 수집기를 가리키는가(엄격한 파서가 못 읽는 옛 plist 는 글자 그대로 찾는다)
OWNS='
import plistlib, sys
from xml.sax.saxutils import escape
plist, collect = sys.argv[1:3]
try:
    with open(plist, "rb") as fh:
        raw = fh.read()
except OSError:
    sys.exit(1)
try:
    args = plistlib.loads(raw).get("ProgramArguments") or []
except Exception:
    sys.exit(0 if ("<string>%s</string>" % escape(collect)).encode("utf-8") in raw else 1)
sys.exit(0 if collect in args else 1)
'
if [ "$OS" = Darwin ]; then
  PLIST="$(state plist)"
  PLIST="${PLIST:-$HOME/Library/LaunchAgents/$LABEL.plist}"
  if [ ! -f "$PLIST" ]; then
    say "plist 없음: $PLIST (건너뜀)"
  elif ! "$PY" -c "$OWNS" "$PLIST" "$COLLECT_SCRIPT"; then
    say "plist $PLIST 는 이 저장소($REPO)의 수집기가 아니다. 다른 자리의 설치라 건드리지 않는다"
  else
    DOMAIN="gui/$(id -u)"
    if [ "$DRY" = 1 ] || "$LAUNCHCTL" print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
      run "$LAUNCHCTL" bootout "$DOMAIN/$LABEL" || say "경고: bootout 실패(이미 내려가 있을 수 있다)"
    fi
    run rm -f "$PLIST"
  fi
else
  n="$(crontab -l 2>/dev/null | grep -c 'pwiki_collect.py' || true)"
  say "리눅스: crontab 은 자동으로 고치지 않는다. pwiki_collect.py 줄 ${n:-0}개. 'crontab -e' 로 지운다."
fi

# ---- 3. 데이터 --------------------------------------------------------------------
step "3. 데이터"
if [ "$PURGE" = 1 ]; then
  if [ "$DATA_DEL" = 1 ]; then
    if [ "$DRY" = 1 ]; then "${DATA[@]}" --apply --dry-run; else "${DATA[@]}" --apply; fi
  fi
  # PWIKI_HOME 을 지우지 않은 경우에도 설치 기록은 뺀다
  if [ "$DRY" = 0 ] && [ -f "$STATE" ]; then run rm -f "$STATE"; fi
else
  if [ -f "$STATE" ]; then run rm -f "$STATE"; fi
  say "남김: $PWIKI_HOME (DB·secrets.local·로그), $PWIKI_VAULT (vault). 지우려면 --purge-data"
fi

step "끝"
exit 0
