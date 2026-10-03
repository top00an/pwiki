#!/bin/bash
# 배포본 생성기: 커밋 하나의 트리를 이력 없는 새 git 저장소(커밋 1개)로 만들고 누출 검사를 돌린다.
#
# 사용: tools/make_dist.sh <커밋> <출력폴더>
# 환경변수:
#   PWIKI_DIST_EMAIL   커밋 작성자 이메일(필수. 저장소에 적지 않는다)
#   PWIKI_DIST_NAME    커밋 작성자 이름(기본 pwiki)
#   PWIKI_DENYLIST     금지 목록 파일(필수, 저장소 밖. 일부러 건너뛰려면 none)
#   PWIKI_DIST_SRC     원본 저장소(기본 이 스크립트가 든 저장소)
#   PWIKI_DIST_PYTHON  누출 검사를 돌릴 python(기본 /usr/bin/python3, 없으면 python3)
# 종료 코드: 0 통과, 1 누출 검사 걸림(출력 폴더는 남긴다), 2 사용 오류.
set -euo pipefail

[ $# -eq 2 ] || { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 2; }
COMMIT="$1"
OUT="$2"
SRC="${PWIKI_DIST_SRC:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)}"
EMAIL="${PWIKI_DIST_EMAIL:-}"
NAME="${PWIKI_DIST_NAME:-pwiki}"
DENY="${PWIKI_DENYLIST:-}"
PY="${PWIKI_DIST_PYTHON:-}"
if [ -z "$PY" ]; then if [ -x /usr/bin/python3 ]; then PY=/usr/bin/python3; else PY="$(command -v python3)"; fi; fi

[ -n "$EMAIL" ] || { echo "PWIKI_DIST_EMAIL 이 없다(커밋 작성자 이메일)" >&2; exit 2; }
[ -n "$DENY" ] || { echo "PWIKI_DENYLIST 가 없다(금지 목록 파일, 건너뛰려면 none)" >&2; exit 2; }
if [ "$DENY" != none ] && [ ! -f "$DENY" ]; then echo "금지 목록 파일이 없다" >&2; exit 2; fi
if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT" 2>/dev/null)" ]; then echo "출력 폴더가 비어 있지 않다: $OUT" >&2; exit 2; fi
SHA="$(git -C "$SRC" rev-parse --verify "$COMMIT^{commit}")" || { echo "커밋을 찾지 못했다: $COMMIT" >&2; exit 2; }

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd -P)"
git -C "$SRC" archive --format=tar "$SHA" | tar -x -C "$OUT"
VER="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$OUT/pwikilib/__init__.py" 2>/dev/null || true)"

# 전역 설정(템플릿 훅·서명·작성자)을 빌리지 않는다
export GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME="$NAME" GIT_AUTHOR_EMAIL="$EMAIL" GIT_COMMITTER_NAME="$NAME" GIT_COMMITTER_EMAIL="$EMAIL"
G=(git -C "$OUT" -c commit.gpgsign=false -c core.hooksPath=/dev/null -c user.name="$NAME" -c user.email="$EMAIL")
git init -q --template= -b main "$OUT"
"${G[@]}" add -A
"${G[@]}" commit -q -m "pwiki ${VER:-dist}"
echo "배포본: $OUT · 커밋 1개 · 파일 $("${G[@]}" ls-files | wc -l | tr -d ' ')개"

ARGS=("$OUT" --single-commit --expect-email "$EMAIL" --expect-name "$NAME")
[ "$DENY" = none ] || ARGS+=(--denylist "$DENY")
set +e
"$PY" "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/leak_check_dist.py" "${ARGS[@]}"
rc=$?
set -e
exit "$rc"
