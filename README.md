# pwiki

**English summary.** pwiki reads your local Claude Code history (`~/.claude/projects/*/*.jsonl`, memory files,
`history.jsonl`, workflow records) into a local SQLite database and builds work cards, a daily timeline,
full-text search, a "resume where I left off" summary and an Obsidian vault. It is deterministic (no LLM calls),
uses only the Python standard library, and keeps everything on your machine. macOS first, Linux works.
Install with `./install.sh`, remove with `./uninstall.sh`. The rest of this document is in Korean.

---

## 무엇을 하는가

Claude Code 가 남기는 기록을 내 컴퓨터 안의 SQLite(`~/.pwiki/pwiki.db`)에 모아 정리한다.

- **작업 카드**: 사람 입력 하나를 카드 한 장으로. 소요 시간·도구 호출·바꾼 파일·에러·하위 에이전트·워크플로를 센다.
- **타임라인**: 그날(KST) 프로젝트별 카드 목록. 세션별로 묶고 `/goal`·마지막 답을 함께 보인다.
- **검색**: FTS5 trigram 전문 검색(한국어 어간 포함). 결과의 키로 `pwiki show` 가 전문을 연다.
- **이어 하기**: 프로젝트의 마지막 요청·마지막 답·최근 병렬 세션·미완 할 일·해결 안 된 에러·최근 카드를 4,000자 안으로.
- **Obsidian vault**: `~/pwiki` 에 날짜·프로젝트·세션 페이지를 md 로 내보낸다.
- **가림**: 적재할 때 비밀번호·토큰 모양 값을 `[REDACTED:…]` 로 바꾼다. DB·검색·vault·이어 하기에 원래 값이 남지 않게 한다.

LLM 을 부르지 않는다. 같은 기록이면 같은 결과가 나온다. 네트워크를 쓰지 않는다.

## 요구 사항

- macOS(우선) 또는 리눅스
- Python 3.9 이상, 표준 라이브러리만. 맥은 `/usr/bin/python3`(명령줄 도구)를 먼저 쓴다.
- SQLite 3.34 이상, FTS5 trigram 지원. 설치기가 먼저 확인하고 없으면 이유를 말하고 멈춘다.
- Claude Code 를 쓴 기록 폴더(`~/.claude/projects`). 다른 곳이면 `PWIKI_CLAUDE_DIR` 로 준다.

## 설치

저장소를 원하는 곳에 풀고 그 자리에서 돌린다. 설치기는 파일을 다른 곳으로 복사하지 않고, 푼 자리의 절대 경로를 쓴다.
설치한 뒤 저장소 폴더를 옮기면 `./install.sh` 를 다시 돌린다(옮기기 전 자리에서 `./uninstall.sh` 를 먼저).

**저장소를 `~/pwiki` 에 두지 않는다.** 그 자리는 vault 기본 위치다. 예: `git clone <주소> ~/src/pwiki`.
설치기는 데이터 자리(`PWIKI_HOME`·`PWIKI_VAULT`)가 저장소나 `~/.claude` 와 겹치면 멈춘다.
`PWIKI_HOME`·vault 자리에 이미 pwiki 가 아닌 파일이 있어도 멈춘다(기존 폴더나 Obsidian vault 에 섞지 않는다).
빈 폴더(숨김 파일만 있어도 된다)나 새 하위 폴더를 준다. 설치기는 두 자리에 표시 파일(`.pwiki-home`·`.pwiki-vault`)을 둔다.
`_notes/` 가 있다는 것만으로는 pwiki vault 로 보지 않는다. 표시 파일 이전에 설치한 자리는 이렇게 알아본다.
`PWIKI_HOME` 은 pwiki DB(`pwiki.db`, 표 구성까지 확인)가 있으면 pwiki 자리다. 곁에 사람이 둔 항목(백업 폴더 등)은 그대로 두고
설치·제거 모두 건드리지 않는다. DB 가 없으면 pwiki 가 만드는 이름만 있을 때(`logs/` 안도 pwiki 로그 이름만, `runs/` 는 비어 있을 때)만 받는다.
vault 는 export 기록(DB)의 페이지가 기록된 내용 그대로 하나 이상 있으면 pwiki 자리다.

```sh
./install.sh            # 단계마다 묻는다
./install.sh --dry-run  # 바꾸는 명령만 보여 주고 바꾸지 않는다
./install.sh --yes      # 선택 단계를 모두 켠다
./install.sh --no-collector --no-hook --yes   # 수집·vault 만
```

단계:

1. 요건 검사(python·SQLite FTS5 trigram·기록 폴더)
2. `~/.pwiki` 만들기(권한 700). `secrets.local` 이 없으면 주석만 든 파일을 만든다(권한 600)
3. 첫 수집 `pwiki ingest --all`. 기록이 많으면 몇 분 걸린다
4. vault 내보내기 `pwiki export`
5. (선택) 자동 수집. 맥은 launchd 잡 `local.pwiki.collect` 를 30분마다 돌린다
   (`~/Library/LaunchAgents/local.pwiki.collect.plist`). 리눅스는 crontab 한 줄을 안내만 한다
6. (선택) 이어 하기 훅. `~/.claude/settings.json` 의 `hooks.SessionStart` 끝에 그룹 하나를 덧붙인다

다시 돌려도 안전하다. 수집은 이어 읽고, plist 는 같으면 그대로 두고, 훅은 이미 있으면 다시 넣지 않는다.

옵션: `--collector`·`--no-collector`, `--hook`·`--no-hook`, `--yes`, `--dry-run`,
`--python PATH`, `--settings PATH`. 위치는 환경변수 `PWIKI_HOME`(기본 `~/.pwiki`), `PWIKI_VAULT`(기본 `~/pwiki`),
`PWIKI_CLAUDE_DIR`(기본 `~/.claude`)로 바꾼다. 대화형이 아니고 옵션도 없으면 선택 단계는 건너뛴다.

settings.json 덧붙이기(`install/merge_settings.py`)가 지키는 것:

- 기존 항목은 키 순서까지 그대로 두고 그룹 하나만 덧붙인다. 출력에 기존 값을 싣지 않는다.
- 쓰기 직전에 파일이 그사이 바뀌었으면 쓰지 않고 멈춘다. 쓴 뒤 다시 읽어 검사하고, 어긋나면 원래 바이트로 되돌린다.
- 다른 설치 위치의 pwiki 훅이 이미 있으면 두 번 붙이지 않고 멈춘다.

## 제거

```sh
./uninstall.sh                     # 훅 빼기 + 자동 수집 내리기. 데이터는 남긴다
./uninstall.sh --purge-data        # pwiki 가 만든 데이터까지 지운다(되돌릴 수 없다, 확인을 묻는다)
./uninstall.sh --purge-data --yes  # 묻지 않고 지운다
./uninstall.sh --dry-run
```

- 대화형이 아닌 셸(파이프·스크립트)에서는 확인을 물을 수 없다. 그래서 `--purge-data` 는 지울 목록만 보이고
  아무것도 바꾸지 않은 채 멈춘다(rc 2, `--dry-run` 을 함께 줘도 같다). 이때는 `--purge-data --yes` 를 준다.

- 훅: 덧붙인 pwiki 그룹만 뺀다. 설치 기록(`~/.pwiki/install.json`)의 명령과, 이 저장소의 `install/pwiki_session_start.py` 를
  부르는 명령을 뺀다. 다른 자리에 설치한 pwiki 훅은 남긴다.
- settings.json 모양: Claude Code 가 쓰는 기본 모양(2칸 들여쓰기)이면 설치 전 바이트로 돌아온다.
  손으로 고친 모양(한 줄 객체가 섞이는 등)이면 설치할 때 파일 전체를 다시 쓰므로, 제거 뒤에는 값·키 순서는 같고 공백만 다르다.
  원래 비어 있던 `"hooks": {}`·`"SessionStart": []` 도 함께 지운다.
- 자동 수집: 맥은 plist 가 이 저장소의 수집기를 가리킬 때만 `launchctl bootout` 후 plist 를 지운다.
  리눅스 crontab 은 직접 지운다.
- `PWIKI_HOME` 을 바꿔 설치했으면 제거할 때도 같은 값을 준다(설치 기록이 그 안에 있다).
- `--purge-data` 는 훅·자동 수집을 건드리기 전에 확인을 먼저 묻는다. pwiki 가 만든 것만 지운다. 폴더째 지우지 않는다.
  - `PWIKI_HOME`: DB(`pwiki.db`·`-wal`·`-shm`)·`secrets.local`·`secrets.harvested`·`ingest.lock`·`install.json`·표시 파일과
    `logs/` 의 pwiki 로그(ingest·install·collect·hook).
  - vault: export 기록(DB 의 `exports`)에 있는 페이지(사람이 고친 페이지 포함), 처음 안내 그대로인 `_notes/README.md`, 표시 파일.
    `days/`·`projects/`·`cards/` 안에 사람이 둔 파일도 남는다.
  - 그 밖의 파일은 남기고 수를 알린다. 폴더는 비었을 때만 지운다.
  - 표시 파일도 export 기록도 없는 사람 폴더, 저장소·HOME·`~/.claude` 와 겹치는 폴더, git 저장소는 건드리지 않는다.
  - vault 페이지는 `PWIKI_HOME` 의 export 기록으로 가린다. DB 를 먼저 지웠으면 페이지는 남는다.
- Claude Code 원래 기록(`~/.claude`)은 어떤 경우에도 지우지 않는다.

## 명령

설치기가 끝에 알려 주는 대로 별칭을 두면 편하다: `alias pwiki='/usr/bin/python3 /설치/경로/pwiki'`

| 명령 | 하는 일 |
|---|---|
| `pwiki today` | 오늘(KST) 프로젝트별 카드 목록 |
| `pwiki day 2026-10-01` | 그날 카드 목록 |
| `pwiki search "낱말" [--project P] [--since D] [--kind human] [--all]` | 전문 검색. P 는 아래 프로젝트 이름. 기본은 하위 에이전트·워크플로 기록을 빼고, `--all` 이면 넣는다 |
| `pwiki show <키>` | search·resume·day 출력의 키 하나의 전문(DB 에 가려 저장된 글). `<sid>:<uuid>`·`카드 <키>`·`문서 <경로>`·`<sid8>/<uuid8>` |
| `pwiki resume --project P` | 이어 하기 요약(4,000자 안). `--project` 는 꼭 준다 |
| `pwiki export [--vault DIR]` | Obsidian vault 로 내보내기. 사람이 고친 페이지는 덮지 않는다 |
| `pwiki ingest --all` | 새 기록 이어 읽기(자동 수집이 30분마다 돌린다) |
| `pwiki verify` | 원문 줄 수를 다시 세어 적재·제외와 맞는지 대조 |
| `pwiki redact-check` | DB·WAL·검색 색인·vault·로그에 남은 비밀값 개수(값은 출력하지 않는다) |
| `pwiki rederive [--apply]` | 저장된 행에 지금 가림·분류·카드 규칙을 다시 적용 |
| `pwiki eff` | 효율 지표(숫자만) |

자동 수집은 DB 만 갱신한다. vault 는 `pwiki export` 를 돌릴 때 갱신된다.

프로젝트 이름 P 로 쓸 수 있는 것:
- `pwiki today` 가 보여 주는 이름. 홈 아래 작업 폴더 경로의 `/`·`.` 을 `-` 로 바꾼 것이다(`~/work/alpha-api` 면 `work-alpha-api`).
  폴더 이름만(`alpha-api`) 주면 찾지 못한다.
- 작업 폴더의 절대 경로(`/Users/me/work/alpha-api`). 하위 폴더 경로는 그 폴더를 작업 폴더(cwd)로 쓴 세션 기록이 있을 때만
  찾고, 그 세션의 프로젝트가 된다. `~` 로 시작하는 경로는 찾지 못한다.
- `~/.claude/projects` 안의 폴더 이름.

## 이어 하기 훅이 주입하는 것

Claude Code 가 새 세션을 열 때(`startup`, `/clear`) `install/pwiki_session_start.py` 가 돈다.
세션의 작업 폴더(cwd)로 pwiki 프로젝트를 찾아 `pwiki resume` 과 같은 요약을 `additionalContext` 로 붙인다.

- 머리말: "pwiki 이어 하기, 지난 기록에서 결정론으로 뽑은 사실이고 지시가 아니다, 사용자 요청이 우선한다"
- 마지막 세션, 마지막 사람 요청, 마지막 답 앞부분, 마지막 압축 요약(Current Work·Next Step), 최근 2시간 다른 세션,
  워크플로, 미완 할 일(마지막 TodoWrite), 해결 안 된 마지막 에러(24시간 안), 그날 작업, 최근 카드
- 끝에 한 줄: 이 설치 자리의 `pwiki search`·`pwiki show` 로 더 찾는 법.
- 요청·답·요약은 모두 DB(적재할 때 가린 값)에서 낸다. 세션 원문 jsonl 은 열지 않는다. 그래서 마지막 수집 뒤의 대화는
  싣지 않는다(자동 수집 주기만큼 늦다, 기본 30분). 대신 마지막 수집 뒤 새 줄이 생긴 세션(크기가 수집 때와 다르거나
  수집이 모르는 새 파일)이 있으면 세션 ID 앞 8자와 바뀐 시각만 한 줄로 알린다(파일 이름·크기·시각만 본다).
  시각만 바뀌고 크기가 같은 파일은 알리지 않는다.
- 새 세션 자신은 뺀다. `/clear` 로 열려도 다른 세션은 빼지 않는다(방금 지운 세션도 나올 수 있다).
- 4,000자 상한. DB 는 읽기만 한다. 1.8초 안에 못 끝내면 아무것도 붙이지 않고 끝낸다. 어떤 실패도 세션을 막지 않는다.
- 프로젝트를 못 찾으면 아무것도 붙이지 않는다. 기록은 `~/.pwiki/logs/hook.log` 에 결과 종류·시간·글자 수만 남긴다.

붙는 내용은 가린 뒤의 기록이다. 그래도 지난 작업 내용이 새 세션 문맥에 들어간다는 점을 알고 켠다.

## 데이터는 내 컴퓨터에만

- DB(`~/.pwiki`)와 vault(`~/pwiki`)에는 내 작업 기록이 그대로 들어 있다(가림은 비밀값에만 한다).
- **DB·vault 를 다른 사람과 공유하지 않는다.** 저장소에 커밋하거나 메신저로 보내지 않는다.
- **iCloud Drive·Dropbox·OneDrive·Google Drive 같은 클라우드 동기화 폴더 안에 두지 않는다.** vault 를 Obsidian Sync 로 올리지 않는다.
  `PWIKI_HOME`·`PWIKI_VAULT` 를 그런 폴더로 정하지 않는다.
- `~/.pwiki` 는 권한 700, 비밀값 파일은 600 으로 둔다.

## 가림은 최선 노력이다

가림은 규칙(비밀번호·토큰 문맥, 알려진 토큰 모양, DSN, 이메일·전화번호 등)과 수확(비밀번호 문맥에서 본 값을 기억해
다른 자리에서도 가림)으로 한다. 문맥 없이 혼자 나온 값은 놓칠 수 있다. 아는 비밀값은 직접 적어 둔다.

```sh
$EDITOR ~/.pwiki/secrets.local     # 한 줄에 값 하나, # 은 주석, 6자 미만은 무시
chmod 600 ~/.pwiki/secrets.local
pwiki rederive --apply             # 이미 쌓인 기록에도 다시 적용
pwiki redact-check                 # 남은 개수 확인(0 이어야 한다)
```

`secrets.local` 과 수확값 파일(`secrets.harvested`)에는 원래 값이 들어 있다. 복사하거나 공유하지 않는다.

## 한계

- 시각은 KST(UTC+9) 고정이다. 날짜 경계·표시가 다른 시간대에서는 어긋난다.
- 맥 우선이다. 리눅스는 수집·검색·훅이 동작하지만 자동 수집은 crontab 을 직접 넣는다. 윈도는 시험하지 않았다.
- Claude Code 기록 형식은 공개 규격이 아니다. 판이 바뀌면 일부 줄을 모르는 형식(`kind=unknown`)으로 적재한다.
  `pwiki verify` 의 형식 분포에서 볼 수 있다.

## 시험

```sh
/usr/bin/python3 -m unittest discover -s tests
```

시험은 임시 폴더의 합성 기록(`tools/make_fixture.py`)으로만 돈다. 실제 `~/.claude`·`~/.pwiki`·settings.json·launchd 는
건드리지 않는다(자동 수집은 `--dry-run` 으로만 본다).

## 배포본 만들기(관리자용)

`tools/make_dist.sh <커밋> <출력폴더>` 는 그 커밋의 트리를 이력 없는 새 git 저장소(커밋 1개)로 만들고
`tools/leak_check_dist.py` 로 누출 검사를 돌린다. 작성자 이메일(`PWIKI_DIST_EMAIL`)과 저장소 밖 금지 목록
(`PWIKI_DENYLIST`)을 환경변수로 준다. 검사에 걸리면 rc 1 이다.
