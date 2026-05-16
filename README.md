# Gandalf

Discord bot for running shell scripts from Discord channels with cron scheduling support.

## Setup

```bash
./setup.sh
```

Prompts for:
- **Discord bot token** (required)
- **Script timeout** — max execution time in seconds (default: 60)

Creates `.env` and installs Python dependencies.

## Permissions

All access control is configured in `permissions.json` at the project root.

```json
{
  "admin_user_id": "123456789",
  "allowed_guilds": ["111111111", "222222222"],
  "commands": {
    "run": {
      "roles": ["Admin", "DevOps"],
      "channels": ["bot-commands"]
    },
    "list": {
      "roles": ["Admin"],
      "channels": []
    }
  },
  "default": {
    "roles": [],
    "channels": []
  }
}
```

| Field | Behavior |
|---|---|
| `admin_user_id` | This Discord user bypasses all permission checks |
| `allowed_guilds` | Bot auto-leaves any server not listed. Empty = allow all (dev mode) |
| `commands.<name>.roles` | Required roles (by name). Empty = admin-only |
| `commands.<name>.channels` | Restrict to these channel names. Empty = any channel |
| `default` | Applied to commands not explicitly listed (including shortcuts) |

Changes to `permissions.json` take effect immediately — no restart needed.

## Usage

```bash
./run.sh     # start bot in background
./stop.sh    # stop bot
```

Logs written to `bot.log`.

## Commands

| Command | Description |
|---|---|
| `/list` | Show available scripts |
| `/run script args? silent?` | Execute a script |
| `/schedule add script cron channel args?` | Add a cron job |
| `/schedule remove job_id` | Remove a cron job |
| `/schedule list` | Show scheduled jobs |

## Scripts

Place `.sh` files in the `scripts/` directory:

- `scripts/common/` — available to all servers
- `scripts/<guild_id>/` — server-specific (shadows common scripts with same name)

Scripts must exist on disk — they cannot be created via Discord.

## Security

- Per-command role and channel restrictions via `permissions.json`
- Global admin user bypass
- Guild allowlisting with auto-leave enforcement
- No `shell=True` — direct subprocess execution only
- Path traversal blocked — bare filenames only
- Timeout enforced on all executions
- Guild isolation for server-specific scripts

## Requirements

- Python 3.10+
- discord.py 2.3+
- python-dotenv
- APScheduler 3.x
