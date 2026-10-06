# Contributing

**English** · 한국어 안내는 아래에 있습니다.

Thanks for helping. A few ground rules keep pwiki what it is.

## Principles

- **Standard library only.** No third-party dependencies, at runtime or in tests.
- **Deterministic.** No LLM calls, no network, no randomness in output. The same history must give the same result.
- **Read-only toward `~/.claude`.** pwiki never edits or deletes Claude Code's files.
- **Redact before storing.** New text paths into the database, search index, vault or logs must go through redaction.

## Never commit real history

Tests and examples must use **synthetic** history only. Generate it with:

```sh
/usr/bin/python3 tools/make_fixture.py --claude-dir /tmp/fake-claude --date 2026-10-02
```

Do not paste real transcripts, real project names, real paths or real secrets into code, tests, issues or pull requests.
README screenshots are made from synthetic data too.

## Running the tests

```sh
/usr/bin/python3 -m unittest discover -s tests
```

Tests run in a temporary folder and never touch your real `~/.claude`, `~/.pwiki`, settings.json or launchd.
Please add a test with every behavior change.

## Pull requests

- Keep each pull request focused on one change.
- Describe what changed and how you verified it (test names, commands you ran).
- Output text is currently Korean. Keep new output consistent with nearby lines.

## Reporting a bug

Open an issue with your OS, Python version, `pwiki --version`, the command you ran, and the output with anything private removed.
For redaction misses or data exposure, follow [SECURITY.md](SECURITY.md) instead.

---

## 한국어

- **표준 라이브러리만**, **결정론(LLM·네트워크·무작위 없음)**, **`~/.claude` 는 읽기만**, **저장 전에 가림** 원칙을 지켜 주세요.
- 시험과 예시는 **합성 기록**(`tools/make_fixture.py`)으로만 만들고, 실제 기록·프로젝트 이름·경로·비밀값은 코드·시험·이슈·PR 어디에도 넣지 않습니다.
- 시험: `/usr/bin/python3 -m unittest discover -s tests`. 동작을 바꾸면 시험을 함께 추가해 주세요.
- 가림 누락이나 데이터 노출은 공개 이슈가 아니라 [SECURITY.md](SECURITY.md) 절차로 알려 주세요.
