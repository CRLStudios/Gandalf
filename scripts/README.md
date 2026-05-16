# Scripts

Place shell scripts here for Gandalf to execute.

## Structure

- `common/` — Available to all servers
- `<guild_id>/` — Available only to that specific server (shadows common scripts with same name)

## Rules

- Scripts must be `.sh` files executable by `/bin/bash`
- Scripts cannot be created via Discord — must be added to disk manually
- Timeout is enforced (default 60s, configurable via `SCRIPT_TIMEOUT` env var)
