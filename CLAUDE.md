# Gandalf — agent notes

Discord bot (discord.py 2.x, Python 3.10 on the server) that runs team operations for an indie game dev team. Domain language and invariants: [CONTEXT.md](CONTEXT.md). User-facing docs: [README.md](README.md).

## Live server

This repo on the Mac is **not** the running bot. The live bot runs on a VPS at `/home/gandalf/github/Gandalf` (SSH in as `gandalf`), host timezone **UTC** — all `/schedule` and `/mergeconfig schedule` times fire in UTC.

- Deploy is manual: commit + push here, then on the server `git pull`, `./stop.sh`, `./run.sh`.
- Nothing supervises the process — no systemd, no auto-restart. If it dies it stays dead until someone notices.
- `run.sh` **truncates `bot.log`** on start. When investigating a crash, capture the log before restarting.
- The bot process only ever runs on the VPS. Verify changes with the standalone tests; starting `bot.py` locally would connect the production bot identity from the wrong machine.

## Testing

Standalone scripts in `tests/`, no pytest, no Discord connection:

```
venv/bin/python tests/local_merge_tests.py
venv/bin/python tests/local_retry_tests.py
```

The numbered scenarios in `local_merge_tests.py` are the de-facto spec for the merge engine — when touching `utils/merge_engine.py`, add the scenario first.

## Safety rails

- `.env` (bot token) and `envs/<guild>.env` (repo access tokens) hold plaintext secrets — gitignored; keep them out of output and commits.
- `permissions.json` is hot-reloaded on every check. Semantics are asymmetric: empty `roles` = **admin-only** (more restrictive), empty `channels` = **anywhere** (less restrictive).
- The commented-out `copy_global_to` lines in `bot.py` are deliberate: re-enabling them duplicates every global command (commit 627e836).
- Autocomplete callbacks bypass command permission checks — every autocomplete must gate itself (see `utils/permissions.py:autocomplete_allowed`).

## Conventions

- Cogs (`cogs/`) own Discord I/O and permission checks; utils (`utils/`) are Discord-free and independently testable. Keep new domain logic in utils.
- Subprocesses: always `asyncio.create_subprocess_exec` wrapped in a timeout — never `shell=True`.
- `check_permissions()` returns `None` when **allowed**, an error string when denied — inverted truthiness; read call sites carefully.
- User errors: guard clauses at the top of the handler, ephemeral reply naming the fixing command. Infrastructure errors: narrow `except discord.HTTPException` with a fallback; `logger.exception` at outer boundaries.
- Every user-supplied identifier is validated against a regex allowlist before touching a path, branch name, or argv.
- Script runs share one execution context everywhere — guild workspace cwd + guild env — whether triggered by `/run`, a shortcut, or a schedule. A new trigger path must pass both to `run_script`.
