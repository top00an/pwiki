# Redaction: what pwiki masks, and what it can miss

**English** · [한국어](REDACTION.ko.md)

pwiki masks secrets **before** anything is written to the database, the search index or the Obsidian vault.
A masked value becomes `[REDACTED:<rule>]`, for example `[REDACTED:aws_key]`.

Redaction is **best effort, not a guarantee.** This page lists what is covered, what is not, and how to check the result yourself.

## How it works

Every line goes through two layers, in this order, on the decoded JSON text.

| Layer | What it matches | Notes |
|---|---|---|
| 1. Known values | Values you list in `~/.pwiki/secrets.local`, plus values pwiki *harvested* from password or token contexts elsewhere | Also catches escaped, URL-encoded, HTML-entity and line-split forms, long fragments of a value (at least 8 characters and 70% of it, containing a digit or symbol), and short hex prefixes of its sha256, sha1 or md5 hash |
| 2. Shapes | Regular expressions for well-known secret formats and password contexts | Listed below |

**Harvesting**: when a shape rule with a value group (for example `PASSWORD=...` or `mysql -p...`) catches a value, pwiki remembers it.
Strong values are masked everywhere afterwards, even with no surrounding context. Weak values (they might be ordinary words) are stored only as hashes.
Harvested values live in `~/.pwiki/secrets.harvested` (mode 600) and nowhere else.

## Shape rules

| Rule name | Catches |
|---|---|
| `private_key` | `-----BEGIN ... PRIVATE KEY-----` blocks |
| `jwt` | JSON Web Tokens (`eyJ...`) |
| `anthropic_key`, `sk_key` | `sk-ant-...`, `sk-...` / `sk-proj-...` API keys |
| `github_token` | `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_` |
| `aws_key` | `AKIA...`, `ASIA...` access key IDs |
| `slack_token` | `xoxb-`, `xoxp-` and similar |
| `google_key` | `AIza...` |
| `apikey_hex` | `apikey_<hex>` style keys |
| `bearer` | `Authorization: Bearer <token>` |
| `dsn` | `scheme://user:password@host` connection strings |
| `mysql_p`, `sshpass`, `basic_auth` | `mysql -p<pw>`, `sshpass -p <pw>`, `curl -u user:pw` and similar CLI passwords |
| `identified_by` | SQL `IDENTIFIED BY '<pw>'` |
| `pw_kv`, `pw_env` | `password=...`, `passwd: ...`, `DB_PW=...`, `*_PASS=...` key-value and environment variable forms |
| `phone` | Korean mobile numbers (`010-...`, `+82 10-...`) |
| `email` | Email addresses (a few generic local parts are kept) |

Private IP addresses are **not** masked on purpose: they are useful work knowledge and rarely secret by themselves.

## What it can miss

- A secret that appears **alone, with no context and no known shape**, for example a random password pasted on its own line, the first time it is seen.
- New token formats that are not in the shape list yet.
- Secrets inside binary attachments or images.
- Anything you typed into a channel pwiki does not read.

If you know a secret, list it. That is the most reliable protection:

```sh
$EDITOR ~/.pwiki/secrets.local     # one value per line, # for comments, values under 6 chars are ignored
chmod 600 ~/.pwiki/secrets.local
pwiki rederive --apply             # re-apply rules to history already stored
```

## Checking the result

```sh
pwiki redact-check
```

`redact-check` uses an **independent** extractor (it does not reuse the redaction rules) to pull password- and token-like values from your original `~/.claude` files, then counts how many of them still appear in the database file bytes, the WAL, the search index, the vault and the logs.
It prints counts and masked shapes only, never the values. The verdict line should read `통과(0건)` (passed, 0).

`pwiki rederive` (without `--apply`) previews what a rule change would alter, read-only, before you apply it.

## Where secrets can still exist

- Claude Code's own transcripts under `~/.claude` are untouched and still contain the original values. pwiki never edits or deletes them.
- `~/.pwiki/secrets.local` and `~/.pwiki/secrets.harvested` contain real values by design. Never copy, sync or share them.
