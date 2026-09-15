# Gandalf

Team-ops Discord bot for an indie game dev team. Began as a script-runner; it is growing into a general automation platform where teammates who don't know git (or the terminal) trigger team operations from Discord. The merge workflow is its most developed subsystem.

## Language

### Platform

**Guild**:
A Discord server the bot serves. The unit of all isolation — every script, env var, workspace, and config is namespaced per guild.
_Avoid_: server (ambiguous with the bot host)

**Script**:
A `.sh` file on the bot host, runnable from Discord via `/run`. A *common* script is available to every guild; a *guild* script belongs to one guild and shadows a common script of the same name. Scripts are added on disk only, never through Discord.

**Shortcut**:
A slash command synthesized at startup from a JSON mapping, aliasing a script (e.g. `/deploy` → `deploy.sh`). Not a real cog command.
_Avoid_: alias, custom command

**Schedule**:
A cron-timed script run that posts its result to a channel. Fires in bot-host local time.
_Avoid_: cron job (that's the mechanism, not the concept)

**Guild env**:
Per-guild `KEY=VALUE` variables injected into script runs. Values are write-only from Discord's perspective — `/env list` shows keys, never values.

**Admin**:
The single Discord user (`admin_user_id`) who bypasses every permission check.

### Merge subsystem

**Registered repo**:
A git repository a guild has registered by name, with URL and optional access token, stored as reserved `REPO_*` guild env vars.

**Workspace**:
The bot's own clone of a registered repo. Disposable — the merge run resets it to remote state; nothing of value lives only there.
_Avoid_: checkout, working copy

**Merge config**:
A guild's settings for `/merge`: which repo, the dev branch, the user branches, the conflict handler, and an optional daily schedule.

**Dev branch**:
The shared integration branch every user branch merges into and is synced back from.
_Avoid_: main, master, trunk

**User branch**:
One team member's personal branch. Members only ever commit to their own.
_Avoid_: team branch, member branch, feature branch

**Merge run**:
One atomic execution of the merge workflow: every user branch merges into dev locally, and only if all are clean does anything push, after which each user branch is fast-forwarded to dev (the *sync-back*). One conflict aborts the entire run with nothing pushed.

**Conflict handler**:
The user pinged when a merge run hits a conflict, with the files and copy-paste commands to resolve it in their own clone. Defaults to the admin.
_Avoid_: ping user

**Round-up**:
The digest of tagged commit-message lines that landed on the dev branch since the baseline, grouped by round-up tag. Posted after a scheduled merge run; previewed on demand without posting.
_Avoid_: changelog, summary, report (that's the merge report)

**Round-up tag**:
A `[Bracketed]` marker starting a commit-message line. Sticky: it files that line and every following line into its round-up section until the next tag. Case-insensitive; lines before any tag stay out of the round-up.
_Avoid_: tag (collides with git tags), label, category

**Baseline**:
The dev-branch commit where the last posted round-up ended; the next round-up covers everything after it. Advances when a round-up posts (scheduled or manual), or deliberately via a skip.

## Invariants

- Exactly one bot process, on one host. All state is files beside the code — no database. Two processes would corrupt state and break the merge guard.
- At most one merge run in flight per guild; manual and scheduled runs share the same guard.
- A merge run is all-or-nothing: no conflict has ever been pushed.
- Permissions are re-read from disk on every check — changes apply with no restart.
- All scheduling fires in bot-host local time.
- Guild isolation is absolute: nothing a guild stores or runs is visible to another guild.
- Repo tokens never appear in command lines, git config, or Discord output.
