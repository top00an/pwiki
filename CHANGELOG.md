# Changelog

All notable changes to pwiki. Versions follow `pwikilib/__init__.py`. Update an existing install with `pwiki update`.

## [0.2.0] - 2026-10-06

### Added
- **Codex CLI support**: `~/.codex/sessions/**/rollout-*.jsonl` and `~/.codex/history.jsonl` go into the same database, so `today`, `day`, `search`, `resume` and `/pwiki` show Codex sessions next to Claude Code ones (marked `Codex`). Nothing else under `~/.codex` is opened (no `auth.json`, no `config.toml`). Override the location with `PWIKI_CODEX_DIR`.
- `pwiki update` and `pwiki update --check`: fast-forward a git-clone install and re-run `install.sh` with the choices recorded at install time. Local edits are never overwritten.
- Claude Code skill: use pwiki inside a session with `/pwiki` or plain questions (installer `--skill`).
- `PWIKI_TZ` display time zone (KST by default; `local`, `UTC`, fixed offsets, IANA names).
- CHANGELOG.

### Changed
- Database schema 1 → 2 (`files.src`, `sessions.src`). Existing databases are upgraded in place on the next ingest; nothing needs to be re-ingested.
- `pwiki verify` and `pwiki redact-check` cover Codex sources too.
- Module docstrings are in English. README covers missing or old Python and SQLite.

## [0.1.0] - 2026-10-04

First public release: local SQLite index of Claude Code history, redaction before storage, daily timeline, full-text search, resume hook, Obsidian export, installer and uninstaller.
