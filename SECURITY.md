# Security policy

**English** · 한국어 안내는 아래에 있습니다.

pwiki handles sensitive material: your coding-agent history and the secrets that may appear in it.
We treat two kinds of problems as security issues:

1. **Redaction misses**: a secret format or context that pwiki should mask but stores in plain text.
2. **Data exposure**: anything that makes pwiki write data outside `~/.pwiki`, the vault or the documented files, send data over the network, or weaken file permissions.

## Reporting

Please **do not open a public issue** for these, and **never include a real secret** in a report.

Use GitHub's private vulnerability reporting: the **Security** tab of this repository → **Report a vulnerability**.
Describe the *shape* of the value (for example "`TOKEN: ` followed by 40 base62 characters") and how it reached the transcript. A fake value with the same shape is ideal.

We aim to acknowledge reports within 7 days.

## Scope notes

- pwiki never sends data over the network and calls no LLM. A finding that it does is always in scope.
- Redaction is best effort; see [docs/REDACTION.md](docs/REDACTION.md) for what is and is not covered. A new pattern request for a common secret format is welcome as a regular issue as long as it contains no real value.
- Claude Code's own transcripts in `~/.claude` are outside pwiki's control.

---

## 한국어

pwiki 는 에이전트 작업 기록과 그 안의 비밀값을 다루는 도구입니다. 아래 두 가지는 보안 문제로 다룹니다.

1. **가림 누락**: pwiki 가 가려야 할 비밀값 형식이나 문맥을 평문으로 저장하는 경우
2. **데이터 노출**: 문서에 적힌 자리 밖에 쓰거나, 네트워크로 보내거나, 파일 권한을 약하게 만드는 경우

공개 이슈로 올리지 말고, **실제 비밀값은 절대 넣지 마세요.** 저장소의 **Security** 탭 → **Report a vulnerability** 로 비공개 신고를 해 주세요. 값의 *모양*(예: "`TOKEN: ` 뒤에 base62 40자")과 기록에 들어간 경로를 적고, 같은 모양의 가짜 값을 주시면 가장 좋습니다. 7일 안에 답하는 것을 목표로 합니다.
