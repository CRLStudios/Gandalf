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
| `/repo add name url token?` | Register a git repository |
| `/repo remove name` | Unregister a repository (removes its URL and token) |
| `/repo list` | Show registered repositories |
| `/repo status name` | Show `git status` of the bot's clone |
| `/env set key value` | Set a per-server env var for script runs |
| `/env remove key` | Remove a per-server env var |
| `/env list` | Show env var keys (values are never displayed) |
| `/merge` | Merge all user branches into dev, then sync them back |
| `/mergeconfig ...` | Configure `/merge` (repo, dev branch, user branches, conflict ping, daily schedule) |
| `/roundup show` | Preview the pending round-up (only you see it) |
| `/roundup post` | Post the pending round-up here for everyone, starting a fresh period |
| `/roundup skip` | Move the round-up baseline to the current dev head without posting |

Shortcut commands (e.g. `/deploy`) can be added by mapping a name to a script
in `shortcuts/global.json` or `shortcuts/<guild_id>.json`; they appear as
their own slash commands on the next restart.

## Team merge (`/merge`)

Built for teams where not everyone knows git: each member works on their own
branch, and one `/merge` command keeps everything in sync.

**Setup (once):**

1. `/repo add name:GAME url:https://github.com/you/game.git token:<PAT>` — register the repo
2. `/mergeconfig repo GAME` — point /merge at it (optional if only one repo)
3. `/mergeconfig dev develop` — the shared development branch
4. `/mergeconfig add alice` (repeat per member) — the branches to merge
5. `/mergeconfig ping @you` — who handles conflicts (defaults to the admin user)
6. `/mergeconfig schedule 03:30 #code` (optional) — also run the merge
   automatically every day at that time (bot-host timezone), posting results
   to the given channel; turn off with `/mergeconfig unschedule`

**What `/merge` does — atomically:**

1. Fetches the repo into the bot's workspace
2. Merges every configured branch into the dev branch **locally**
3. If **any** merge conflicts: everything is aborted, **nothing is pushed**, and
   the conflict-handler is pinged with the branch, the conflicting files, and
   copy-paste commands to resolve it in their own clone
4. If all merges are clean: dev is pushed, then every user branch is
   fast-forwarded to match dev — after a clean run the whole team has identical
   history and everyone just pulls

Branches not found on the remote are reported but don't block the run. If
someone pushes new work mid-run their branch is left alone and catches up on
the next `/merge`. Only one merge can run per server at a time — scheduled
daily runs use the exact same flow and guard as a manual `/merge`, so they
can never collide, and conflicts in a scheduled run ping the handler the
same way.

Repo access tokens are passed to git via an askpass helper — they are never
written into `.git/config` or command lines. If your repo uses Git LFS,
install `git-lfs` on the bot host.

### Daily round-up

After each scheduled merge that lands changes, the bot posts a **round-up**:
a digest of what went in since the last one, grouped by tags. Start a line
of your commit message with a `[Tag]` to include it:

```
[Dev] Fixed double-jump through platforms
[Art] New forest tileset
```

- Tags are case-insensitive (`[dev]`, `[Dev]`, `[DEV]` group together) and
  displayed Title-cased; any tag name works — no fixed list.
- Every line of the commit message is scanned, so one commit can feed
  several sections. With several tags on one line, the first wins.
- Untagged lines stay out of the report; merge commits and the bot's own
  commits are always skipped.
- Days with nothing tagged post nothing. Changes merged manually with
  `/merge` mid-day appear in that day's scheduled round-up.
- `/roundup show` shows you (privately) what's accumulated so far, without
  posting or affecting the daily report.
- `/roundup post` publishes the pending round-up right now in the current
  channel and starts a fresh period — handy right after resolving a
  conflict, so the team doesn't wait for the next scheduled merge.
- `/roundup skip` moves the baseline to the current dev head without
  posting: everything accumulated so far is dropped from future reports
  (the reply tells you how many entries were skipped). Also works before
  the first scheduled merge, to start the round-up clock "from now".

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
