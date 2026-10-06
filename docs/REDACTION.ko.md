# 비밀값 가림: 무엇을 가리고, 무엇을 놓칠 수 있나

[English](REDACTION.md) · **한국어**

pwiki 는 DB·검색 색인·Obsidian vault 에 쓰기 **전에** 비밀값을 가립니다.
가린 값은 `[REDACTED:<규칙>]` 으로 바뀝니다. 예: `[REDACTED:aws_key]`.

가림은 **최선 노력이고, 보장이 아닙니다.** 이 문서는 무엇을 잡고, 무엇을 못 잡고, 결과를 어떻게 직접 확인하는지 적습니다.

## 동작 방식

모든 줄은 JSON 을 푼 글에 아래 두 단계를 차례로 거칩니다.

| 단계 | 잡는 것 | 참고 |
|---|---|---|
| 1. 알려진 값 | `~/.pwiki/secrets.local` 에 직접 적은 값, 그리고 다른 자리의 비밀번호·토큰 문맥에서 **수확한** 값 | 이스케이프·URL 인코딩·HTML 엔티티·줄 나눔 형태, 값의 긴 조각(8자 이상이면서 70% 이상, 숫자나 특수문자 포함), sha256·sha1·md5 해시의 짧은 16진 앞부분까지 잡습니다 |
| 2. 형태 | 잘 알려진 비밀값 형식과 비밀번호 문맥을 정규식으로 | 아래 표 |

**수확**: 값 자리가 있는 형태 규칙(예: `PASSWORD=...`, `mysql -p...`)이 값을 잡으면 pwiki 가 기억합니다.
강한 값은 그 뒤로 문맥이 없어도 어디서나 가립니다. 약한 값(평범한 낱말일 수 있음)은 해시만 기억합니다.
수확값은 `~/.pwiki/secrets.harvested`(권한 600)에만 있습니다.

## 형태 규칙

| 규칙 이름 | 잡는 것 |
|---|---|
| `private_key` | `-----BEGIN ... PRIVATE KEY-----` 블록 |
| `jwt` | JSON Web Token(`eyJ...`) |
| `anthropic_key`, `sk_key` | `sk-ant-...`, `sk-...`·`sk-proj-...` API 키 |
| `github_token` | `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_` |
| `aws_key` | `AKIA...`, `ASIA...` 액세스 키 ID |
| `slack_token` | `xoxb-`, `xoxp-` 등 |
| `google_key` | `AIza...` |
| `apikey_hex` | `apikey_<16진>` 형태 키 |
| `bearer` | `Authorization: Bearer <토큰>` |
| `dsn` | `scheme://user:password@host` 접속 문자열 |
| `mysql_p`, `sshpass`, `basic_auth` | `mysql -p<비밀번호>`, `sshpass -p <비밀번호>`, `curl -u user:pw` 같은 명령줄 비밀번호 |
| `identified_by` | SQL `IDENTIFIED BY '<비밀번호>'` |
| `pw_kv`, `pw_env` | `password=...`, `passwd: ...`, `DB_PW=...`, `*_PASS=...` 같은 키-값·환경변수 형태 |
| `phone` | 한국 휴대전화 번호(`010-...`, `+82 10-...`) |
| `email` | 이메일 주소(흔한 일반 주소 일부는 남김) |

사설 IP 는 **일부러 가리지 않습니다.** 작업에 필요한 정보이고, 그 자체로 비밀인 경우가 드물기 때문입니다.

## 놓칠 수 있는 것

- **문맥도 없고 알려진 형태도 아닌 값이 처음 나올 때.** 예: 비밀번호 하나만 덩그러니 붙여 넣은 줄.
- 아직 형태 목록에 없는 새 토큰 형식.
- 바이너리 첨부나 이미지 안의 비밀값.
- pwiki 가 읽지 않는 경로로 입력한 내용.

아는 비밀값은 직접 적어 두는 것이 가장 확실합니다.

```sh
$EDITOR ~/.pwiki/secrets.local     # 한 줄에 값 하나, # 은 주석, 6자 미만은 무시
chmod 600 ~/.pwiki/secrets.local
pwiki rederive --apply             # 이미 쌓인 기록에도 다시 적용
```

## 결과 확인

```sh
pwiki redact-check
```

`redact-check` 는 가림 규칙을 쓰지 않는 **독립** 추출기로 원본 `~/.claude` 파일에서 비밀번호·토큰 같은 값을 뽑고, 그 값이 DB 파일 바이트·WAL·검색 색인·vault·로그에 몇 번 남았는지 셉니다.
값은 출력하지 않고 개수와 가린 모양만 보여 줍니다. 판정 줄이 `통과(0건)` 이어야 합니다.

`pwiki rederive`(`--apply` 없이)는 규칙을 바꿨을 때 무엇이 바뀔지 읽기 전용으로 미리 보여 줍니다.

## 비밀값이 여전히 남는 곳

- Claude Code 원래 기록(`~/.claude`)은 그대로이고 원래 값이 들어 있습니다. pwiki 는 그 파일을 고치거나 지우지 않습니다.
- `~/.pwiki/secrets.local` 과 `~/.pwiki/secrets.harvested` 에는 설계상 원래 값이 들어 있습니다. 복사·동기화·공유하지 않습니다.
