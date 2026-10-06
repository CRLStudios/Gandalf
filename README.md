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
| `/merge dev?` | Merge a merge set's user branches into its dev branch, then sync them back (default set if `dev` is omitted) |
| `/mergeconfig ...` | Configure `/merge` (repo, merge sets, user branches, conflict ping, daily schedule, release branch) |
| `/roundup show dev?` | Preview the pending round-up (only you see it) |
| `/roundup post dev?` | Post the pending round-up here for everyone, starting a fresh period |
| `/roundup skip dev?` | Move the round-up baseline to the current dev head without posting |
| `/patchnotes version? branch? post?` | Tagged changes that went into a version, since the version before it |

Shortcut commands (e.g. `/deploy`) can be added by mapping a name to a script
in `shortcuts/global.json` or `shortcuts/<guild_id>.json`; they appear as
their own slash commands on the next restart.

## Team merge (`/merge`)

Built for teams where not everyone knows git: each member works on their own
branch, and one `/merge` command keeps everything in sync.

**Setup (once):**

1. `/repo add name:GAME url:https://github.com/you/game.git token:<PAT>` — register the repo
2. `/mergeconfig repo GAME` — point /merge at it (optional if only one repo)
3. `/mergeconfig create develop` — a merge set for the shared development branch
4. `/mergeconfig add alice` (repeat per member) — the branches to merge
5. `/mergeconfig ping @you` — who handles conflicts (defaults to the admin user)
6. `/mergeconfig schedule 03:30 #code` (optional) — also run the merge
   automatically every day at that time (bot-host timezone), posting results
   to the given channel; turn off with `/mergeconfig unschedule`

`/mergeconfig show` lists everything that is configured.

### Merge sets

A **merge set** is one dev branch plus the user branches merged into it. The
first set you create is the **default set**: `/merge`, `/roundup` and
`/mergeconfig` act on it whenever `dev:` is left out. Create as many more as
you need — for example a patch line next to main development:

```
/mergeconfig create patch-development
/mergeconfig add alice-patch dev:patch-development
/mergeconfig add bob-patch dev:patch-development
/mergeconfig schedule 04:00 #code dev:patch-development    (optional)
/merge dev:patch-development
```

- Each set has its own user branches, daily schedule and round-up. The repo
  and the conflict handler are shared by all sets.
- A branch belongs to one set only, and a dev branch can't be a user branch
  in any set — synced back from two dev branches, a branch would carry each
  one's history into the other.
- Sets are independent: nothing flows from `patch-development` into
  `develop` unless someone merges it by hand.
- `/mergeconfig default <dev>` moves the default, `/mergeconfig rename <dev>
  <new_branch>` changes a set's dev branch (keeping its branches, schedule
  and round-up baseline), and `/mergeconfig delete <dev>` removes a set from
  the config without touching the remote. The default set can only be
  deleted last.

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
the next `/merge`. Only one merge can run per server at a time, whichever
set it is for — scheduled daily runs use the exact same flow and guard as a
manual `/merge`, so they can never collide, and conflicts in a scheduled run
ping the handler the same way. A manual `/merge` during a run is turned
away; a scheduled run waits its turn instead (up to an hour), so two sets
can share a schedule time.

Repo access tokens are passed to git via an askpass helper — they are never
written into `.git/config` or command lines. If your repo uses Git LFS,
install `git-lfs` on the bot host.

During long transfers (the first clone, a big fetch or push) the status
message updates live with elapsed time and git's own progress, e.g.
`Receiving objects: 42% (8123/19301), 210.4 MiB | 12.3 MiB/s`. These
transfers are killed only if git goes completely silent for `GIT_TIMEOUT`
seconds (default 900) — a slow but moving download is never cut off.

### Daily round-up

After each scheduled merge that lands changes, the bot posts a **round-up**:
a digest of what went in since the last one, grouped by tags. Every merge
set keeps its own round-up, and the `/roundup` commands take the same
optional `dev:` as `/merge`. Start a line of your commit message with a
`[Tag]` to include it:

```
[Dev] Fixed double-jump through platforms
Reworked coyote time
[Art] New forest tileset
```

- Tags are case-insensitive (`[dev]`, `[Dev]`, `[DEV]` group together) and
  displayed Title-cased; any tag name works — no fixed list.
- A tag is sticky: it covers its own line and every line after it until
  the next tag, each becoming its own item (above: two Dev items, one
  Art). One commit can feed several sections; with several tags on one
  line, the first wins.
- Lines before the first tag stay out of the report; merge commits and
  the bot's own commits are always skipped. A leading `* ` on a line is
  stripped — the report draws its own bullets.
- A round-up too big for one Discord message continues across several
  ("📋 Daily Round-up — Page 1/2", "Page 2/2", …); nothing is cut off.
- Days with nothing tagged post nothing. Changes merged manually with
  `/merge` mid-day appear in that day's scheduled round-up.
- Work carried by hand from one merge set into another (e.g. patch fixes
  merged into `develop`) shows up in both sets' round-ups — it is new to
  each dev branch.
- `/roundup show` shows you (privately) what's accumulated so far, without
  posting or affecting the daily report.
- `/roundup post` publishes the pending round-up right now in the current
  channel and starts a fresh period — handy right after resolving a
  conflict, so the team doesn't wait for the next scheduled merge.
- `/roundup skip` moves the baseline to the current dev head without
  posting: everything accumulated so far is dropped from future reports
  (the reply tells you how many entries were skipped). Also works before
  the first scheduled merge, to start the round-up clock "from now".

### Patch notes

`/patchnotes` is the same digest cut by **release** instead of by day: the
tagged commit lines that went into a version, since the version before it.
A version is a git tag of the form `v1.0.3` (`v` and three numbers) on the
release branch — set that once with `/mergeconfig release main`.

```
/patchnotes                      the version at the head of the release branch
/patchnotes version:v1.0.2       an older version
/patchnotes branch:patch-release another branch, just this once
/patchnotes post:True            publish in this channel instead of showing only you
```

- If the head of the branch is tagged `v1.0.3`, the notes cover everything
  after `v1.0.2` up to and including the `v1.0.3` commit. If the head isn't
  tagged yet, you get the **unreleased changes** since the latest version.
- The "version before" is the highest lower version in that version's own
  history, so a `v1.0.4` hotfix tagged after `v1.1.0` shipped doesn't become
  the baseline for `v1.1.1`. The first version covers the whole history up
  to its tag.
- Only `vX.Y.Z` tags count: `v1.0.3-rc1`, `build-42` and the like are
  ignored. `version:` must be a tag in the history of the branch you're
  looking at.
- Commit lines are picked up exactly as for the round-up (`[Tag]` lines,
  sticky tags, merge and bot commits skipped, long reports paginated).
- Nothing is remembered between runs — the git tags are the only state, so
  the same version always gives the same notes, and patch notes never
  affect the round-up.
- Anyone on the team can view patch notes privately; `post:True` has its
  own `patchnotes post` entry in `permissions.json`, so publishing can be
  restricted separately from viewing.

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
