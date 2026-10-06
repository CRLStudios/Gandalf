import asyncio
import logging
import re
import time

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
from discord.ext import commands

from utils.gitops import GitError, build_git_env, git
from utils.merge_config import (
    MergeConfigError,
    add_branch,
    all_guild_ids,
    branch_owner,
    create_set,
    delete_set,
    load_merge_config,
    remove_branch,
    rename_set,
    resolve_set,
    save_merge_config,
    set_default,
)
from utils.merge_engine import BranchResult, MergeEngine, MergeReport
from utils.patchnotes import (
    PatchNotesError,
    PatchRange,
    fetch_refs,
    normalize_version,
    resolve_range,
    version_tags,
)
from utils.permissions import (
    autocomplete_allowed,
    check_permissions,
    get_admin_user_id,
    require_permissions,
)
from utils.repos import repo_credentials, repo_names, repo_workspace
from utils.roundup import collect_roundup, format_roundup_pages

logger = logging.getLogger(__name__)

BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

EMBED_DESC_LIMIT = 4096

DEV_OPTION = "Dev branch of the merge set (default set if omitted)"

# A scheduled run that finds another merge in flight waits its turn.
SCHEDULE_WAIT_POLL = 5      # seconds between checks
SCHEDULE_WAIT_LIMIT = 3600  # give up (and say so) after this long

STATUS_LINES = {
    "merged": "🔀 `{branch}` — merged (+{commits} commit{s})",
    "up_to_date": "✔ `{branch}` — already up to date",
    "missing": "⚠ `{branch}` — not found on the remote (has this member pushed yet?)",
}

SYNC_NOTES = {
    "synced": "",
    "in_sync": "",
    "sync_skipped": " · ⚠ new work was pushed mid-run, it will be picked up next `/merge`",
    "sync_failed": " · ⚠ could not sync this branch back (see bot.log) — it will retry next `/merge`",
}


def _valid_branch(name: str) -> bool:
    return bool(BRANCH_PATTERN.match(name)) and ".." not in name and not name.endswith((".lock", "/", "."))


class MergeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Guilds with a merge in flight. A plain set mutated before any await
        # is race-free on the event loop, unlike check-then-acquire on a Lock
        # (which would silently queue a second run instead of rejecting it).
        self._running: set[int] = set()
        self.scheduler = AsyncIOScheduler()

    async def cog_load(self):
        for guild_id in all_guild_ids():
            for dev, merge_set in load_merge_config(guild_id)["sets"].items():
                if not merge_set["schedule"]:
                    continue
                try:
                    self._register_schedule(guild_id, dev, merge_set["schedule"])
                except Exception:
                    logger.exception(
                        "Could not register merge schedule for guild %s set %s", guild_id, dev
                    )
        self.scheduler.start()

    async def cog_unload(self):
        self.scheduler.shutdown(wait=False)

    # --------------------------------------------------------------- scheduling

    def _job_id(self, guild_id: int, dev: str) -> str:
        return f"merge_{guild_id}_{dev}"

    def _register_schedule(self, guild_id: int, dev: str, schedule: dict):
        hour, minute = (int(p) for p in schedule["time"].split(":"))
        self.scheduler.add_job(
            self._scheduled_merge,
            CronTrigger(hour=hour, minute=minute),
            id=self._job_id(guild_id, dev),
            args=[guild_id, dev],
            replace_existing=True,
        )

    def _unregister_schedule(self, guild_id: int, dev: str):
        if self.scheduler.get_job(self._job_id(guild_id, dev)):
            self.scheduler.remove_job(self._job_id(guild_id, dev))

    async def _claim_turn(self, guild_id: int) -> bool:
        """Wait for the guild's merge guard, then take it. False if the wait timed out.

        Sets share one workspace, so scheduled runs queue behind whatever is
        in flight instead of skipping — two sets may share a schedule time.
        The final check and the add have no await between them, which keeps
        the guard race-free.
        """
        waited = 0
        while guild_id in self._running:
            if waited >= SCHEDULE_WAIT_LIMIT:
                return False
            await asyncio.sleep(SCHEDULE_WAIT_POLL)
            waited += SCHEDULE_WAIT_POLL
        self._running.add(guild_id)
        return True

    async def _scheduled_merge(self, guild_id: int, dev: str):
        claimed = await self._claim_turn(guild_id)
        try:
            # Loaded only after the wait: the set may have been rescheduled,
            # renamed or deleted while this run was queued.
            config = load_merge_config(guild_id)
            schedule = config["sets"].get(dev, {}).get("schedule")
            if not schedule:
                # Unscheduled after the job was registered — clean up defensively.
                self._unregister_schedule(guild_id, dev)
                return

            channel = self.bot.get_channel(schedule["channel_id"])
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(schedule["channel_id"])
                except discord.HTTPException:
                    logger.warning(
                        "Scheduled merge for guild %s set %s: channel %s unavailable",
                        guild_id, dev, schedule["channel_id"],
                    )
                    return

            if not claimed:
                await channel.send(
                    f"⏰ Scheduled merge of `{dev}` skipped — another merge was still running."
                )
                return
            try:
                repo, _ = self._resolve_target(guild_id, config, dev)
            except MergeConfigError as e:
                await channel.send(f"⏰ Scheduled merge of `{dev}` skipped: {e}")
                return
            logger.info(
                "Scheduled merge starting for guild %s (repo %s, set %s)", guild_id, repo, dev
            )
            status_msg = await channel.send(f"⏰ Daily merge run started for `{repo}` → `{dev}`…")
            report = await self._execute(guild_id, config, repo, dev, channel, status_msg)
            await self._post_roundup(guild_id, repo, dev, report, channel)
        except Exception:
            logger.exception("Scheduled merge failed for guild %s set %s", guild_id, dev)
        finally:
            if claimed:
                self._running.discard(guild_id)

    # ---------------------------------------------------------------- round-up

    async def _post_roundup(
        self,
        guild_id: int,
        repo: str,
        dev: str,
        report: MergeReport,
        channel: discord.abc.Messageable,
    ):
        """After a scheduled run: post the set's tagged-changes digest, if any.

        The baseline advances only when a round-up actually posts, so
        commits land in exactly one report even across manual merges,
        empty days, and failed sends.
        """
        if not report.ok or report.dev_head is None:
            return
        config = load_merge_config(guild_id)
        state = config["sets"].get(dev, {}).get("roundup")
        if not state or state.get("repo") != repo or not state.get("baseline"):
            # First run (or the repo was re-pointed): record silently.
            self._save_baseline(guild_id, dev, repo, report.dev_head)
            return
        if state["baseline"] == report.dev_head:
            return

        workspace = repo_workspace(guild_id, repo)
        _, token = repo_credentials(guild_id, repo)
        try:
            groups = await collect_roundup(
                workspace, build_git_env(token), state["baseline"], report.dev_head
            )
        except GitError:
            # Baseline vanished from history (force-pushed remote) — start over.
            logger.warning("Round-up baseline invalid for guild %s set %s; resetting", guild_id, dev)
            self._save_baseline(guild_id, dev, repo, report.dev_head)
            return

        pages = format_roundup_pages(groups, EMBED_DESC_LIMIT)
        if not pages:
            return  # nothing tagged — silent, baseline stays

        try:
            for embed in self._roundup_embeds(
                "📋 Daily Round-up", pages, f"{repo} · {dev} · changes since the last round-up"
            ):
                await channel.send(embed=embed)
        except discord.HTTPException:
            # Baseline stays even if some pages made it out: a duplicated
            # page tomorrow beats silently losing the rest of the report.
            logger.exception("Failed to post round-up for guild %s set %s", guild_id, dev)
            return
        self._save_baseline(guild_id, dev, repo, report.dev_head)

    roundup_group = app_commands.Group(
        name="roundup", description="Round-up of tagged changes on the dev branch", guild_only=True
    )

    @roundup_group.command(name="show", description="Preview the pending round-up (only you see it)")
    @app_commands.describe(dev=DEV_OPTION)
    @require_permissions()
    async def roundup_show(self, interaction: discord.Interaction, dev: str | None = None):
        await self._roundup_command(interaction, "show", dev)

    @roundup_group.command(name="post", description="Post the pending round-up here for everyone, starting a fresh period")
    @app_commands.describe(dev=DEV_OPTION)
    @require_permissions()
    async def roundup_post(self, interaction: discord.Interaction, dev: str | None = None):
        await self._roundup_command(interaction, "post", dev)

    @roundup_group.command(name="skip", description="Move the round-up baseline to the current dev head without posting")
    @app_commands.describe(dev=DEV_OPTION)
    @require_permissions()
    async def roundup_skip(self, interaction: discord.Interaction, dev: str | None = None):
        await self._roundup_command(interaction, "skip", dev)

    @staticmethod
    def _roundup_embeds(title: str, pages: list[str], footer: str) -> list[discord.Embed]:
        total = len(pages)
        embeds = []
        for i, description in enumerate(pages, 1):
            embed = discord.Embed(
                title=title if total == 1 else f"{title} — Page {i}/{total}",
                description=description,
                color=0x5865F2,
            )
            embed.set_footer(text=footer)
            embeds.append(embed)
        return embeds

    def _save_baseline(self, guild_id: int, dev: str, repo: str, tip: str):
        # Reload instead of reusing the caller's config: a /mergeconfig edit
        # made in the meantime must not be clobbered by this save.
        config = load_merge_config(guild_id)
        if dev not in config["sets"]:
            return  # the set was deleted or renamed mid-run — nothing to track
        config["sets"][dev]["roundup"] = {"repo": repo, "baseline": tip}
        save_merge_config(guild_id, config)

    async def _roundup_command(self, interaction: discord.Interaction, mode: str, requested: str | None):
        guild_id = interaction.guild_id
        if guild_id in self._running:
            await interaction.response.send_message(
                "A merge is running for this server — try again in a minute.", ephemeral=True
            )
            return
        self._running.add(guild_id)
        try:
            config = load_merge_config(guild_id)
            try:
                repo, dev = self._resolve_target(guild_id, config, requested)
            except MergeConfigError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return
            state = config["sets"][dev]["roundup"]
            has_baseline = bool(state and state.get("repo") == repo and state.get("baseline"))
            if not has_baseline and mode != "skip":
                await interaction.response.send_message(
                    "No round-up baseline yet — wait for the first scheduled merge, "
                    "or start one from the current head with `/roundup skip`.",
                    ephemeral=True,
                )
                return
            workspace = repo_workspace(guild_id, repo)
            if not (workspace / ".git").exists():
                await interaction.response.send_message(
                    "The bot hasn't cloned this repo yet — run `/merge` first.", ephemeral=True
                )
                return

            # The public post is sent separately via the channel, so the
            # interaction side of every mode stays ephemeral.
            await interaction.response.defer(ephemeral=True)
            _, token = repo_credentials(guild_id, repo)
            env = build_git_env(token)
            try:
                await git("fetch", "--prune", "origin", cwd=workspace, env=env)
                rc, out, _ = await git(
                    "rev-parse", "--verify", "--quiet",
                    f"refs/remotes/origin/{dev}",
                    cwd=workspace, env=env, check=False,
                )
                if rc != 0:
                    await interaction.followup.send(
                        f"Development branch `{dev}` was not found on the remote.",
                        ephemeral=True,
                    )
                    return
                tip = out.strip()

                if mode == "skip":
                    await self._roundup_skip_body(
                        interaction, guild_id, repo, dev, state, has_baseline, workspace, env, tip
                    )
                    return

                groups = await collect_roundup(workspace, env, state["baseline"], tip)
            except GitError as e:
                await interaction.followup.send(f"Could not read the repo: {e}", ephemeral=True)
                return

            pages = format_roundup_pages(groups, EMBED_DESC_LIMIT)
            if mode == "show":
                if not pages:
                    pages = ["No tagged changes since the last round-up."]
                for embed in self._roundup_embeds(
                    "📋 Round-up preview", pages, f"{repo} · {dev} · posts with the next daily merge"
                ):
                    await interaction.followup.send(embed=embed, ephemeral=True)
                return

            # mode == "post"
            if not pages:
                await interaction.followup.send(
                    "No tagged changes since the last round-up — nothing to post.", ephemeral=True
                )
                return
            try:
                for embed in self._roundup_embeds(
                    "📋 Round-up", pages, f"{repo} · {dev} · changes since the last round-up"
                ):
                    await interaction.channel.send(embed=embed)
            except discord.HTTPException:
                # Baseline stays even if some pages made it out: a duplicated
                # page next time beats silently losing the rest of the report.
                logger.exception("Failed to post round-up for guild %s set %s", guild_id, dev)
                await interaction.followup.send(
                    "Could not post the full round-up in this channel.", ephemeral=True
                )
                return
            self._save_baseline(guild_id, dev, repo, tip)
            await interaction.followup.send(
                "Round-up posted — the next one covers changes from now on.", ephemeral=True
            )
        finally:
            self._running.discard(guild_id)

    async def _roundup_skip_body(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        repo: str,
        dev: str,
        state: dict | None,
        has_baseline: bool,
        workspace,
        env: dict,
        tip: str,
    ):
        if not has_baseline:
            self._save_baseline(guild_id, dev, repo, tip)
            await interaction.followup.send(
                "Round-up baseline started at the current "
                f"`{dev}` head — the next round-up covers changes from now on.",
                ephemeral=True,
            )
            return
        if state["baseline"] == tip:
            await interaction.followup.send(
                "The baseline is already at the current head — nothing to skip.", ephemeral=True
            )
            return
        # Count what's being discarded so a fat-fingered skip is visible.
        note = ""
        try:
            groups = await collect_roundup(workspace, env, state["baseline"], tip)
            skipped = sum(len(entries) for entries in groups.values())
            note = f"**{skipped}** tagged entr{'y' if skipped == 1 else 'ies'} skipped."
        except GitError:
            note = "the old baseline was missing from history, skipped entries could not be counted."
        self._save_baseline(guild_id, dev, repo, tip)
        await interaction.followup.send(
            f"Baseline moved to the current `{dev}` head — {note}", ephemeral=True
        )

    # ------------------------------------------------------------- /patchnotes

    @app_commands.command(
        name="patchnotes",
        description="Tagged changes that went into a version, since the version before it",
    )
    @app_commands.describe(
        version="Version tag, e.g. v1.0.3 (default: whatever is at the head of the branch)",
        branch="Branch to look at (default: the release branch)",
        post="Post the notes here for everyone instead of showing them only to you",
    )
    @app_commands.guild_only()
    @require_permissions()
    async def patchnotes(
        self,
        interaction: discord.Interaction,
        version: str | None = None,
        branch: str | None = None,
        post: bool = False,
    ):
        guild_id = interaction.guild_id
        if post:
            # Publishing has its own permissions.json entry, like /roundup post.
            denied = check_permissions(interaction, "patchnotes post")
            if denied:
                await interaction.response.send_message(
                    f"Posting patch notes for everyone is restricted — {denied} "
                    "Leave `post` off to see them yourself.",
                    ephemeral=True,
                )
                return
        tag = None
        if version is not None:
            tag = normalize_version(version)
            if tag is None:
                await interaction.response.send_message(
                    "A version looks like `v1.0.3` — `v`, then three numbers.", ephemeral=True
                )
                return
        config = load_merge_config(guild_id)
        branch = branch or config["release_branch"]
        if not branch:
            await interaction.response.send_message(
                "No release branch set — set one with `/mergeconfig release`, or pass `branch:`.",
                ephemeral=True,
            )
            return
        if not _valid_branch(branch):
            await interaction.response.send_message("That doesn't look like a valid branch name.", ephemeral=True)
            return
        try:
            repo = self._resolve_repo(guild_id, config)
        except MergeConfigError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        # Fetching shares the workspace with merge runs, so it takes the same guard.
        if guild_id in self._running:
            await interaction.response.send_message(
                "A merge is running for this server — try again in a minute.", ephemeral=True
            )
            return
        self._running.add(guild_id)
        try:
            workspace = repo_workspace(guild_id, repo)
            if not (workspace / ".git").exists():
                await interaction.response.send_message(
                    "The bot hasn't cloned this repo yet — run `/merge` first.", ephemeral=True
                )
                return

            # The public post is sent separately via the channel, so the
            # interaction side stays ephemeral either way.
            await interaction.response.defer(ephemeral=True)
            _, token = repo_credentials(guild_id, repo)
            env = build_git_env(token)
            try:
                await fetch_refs(workspace, env)
                notes = await resolve_range(workspace, env, branch, tag)
                groups = await collect_roundup(workspace, env, notes.base, notes.tip)
            except PatchNotesError as e:
                await interaction.followup.send(str(e), ephemeral=True)
                return
            except GitError as e:
                await interaction.followup.send(f"Could not read the repo: {e}", ephemeral=True)
                return

            title, span, empty = self._patchnotes_labels(notes)
            footer = f"{repo} · {branch} · {span}"
            pages = format_roundup_pages(groups, EMBED_DESC_LIMIT)
            if not post:
                for embed in self._roundup_embeds(title, pages or [empty], footer):
                    await interaction.followup.send(embed=embed, ephemeral=True)
                return

            if not pages:
                await interaction.followup.send(f"{empty} Nothing to post.", ephemeral=True)
                return
            try:
                for embed in self._roundup_embeds(title, pages, footer):
                    await interaction.channel.send(embed=embed)
            except discord.HTTPException:
                logger.exception("Failed to post patch notes for guild %s", guild_id)
                await interaction.followup.send(
                    "Could not post the full patch notes in this channel.", ephemeral=True
                )
                return
            await interaction.followup.send("Patch notes posted.", ephemeral=True)
        finally:
            self._running.discard(guild_id)

    @staticmethod
    def _patchnotes_labels(notes: PatchRange) -> tuple[str, str, str]:
        """(embed title, footer span, reply when nothing is tagged) for a range."""
        if notes.version is None:
            return (
                f"📝 Unreleased changes since {notes.previous}",
                f"since {notes.previous}",
                f"No tagged changes since `{notes.previous}`.",
            )
        title = f"📝 Patch notes — {notes.version}"
        if notes.previous is None:
            return (
                title,
                f"everything up to {notes.version}",
                f"No tagged changes up to `{notes.version}`.",
            )
        return (
            title,
            f"{notes.previous} → {notes.version}",
            f"No tagged changes between `{notes.previous}` and `{notes.version}`.",
        )

    # ------------------------------------------------------------------ /merge

    @app_commands.command(
        name="merge",
        description="Merge everyone's branches into the dev branch and sync them all back up",
    )
    @app_commands.describe(dev="Dev branch of the merge set to run (default set if omitted)")
    @app_commands.guild_only()
    @require_permissions()
    async def merge(self, interaction: discord.Interaction, dev: str | None = None):
        guild_id = interaction.guild_id
        if guild_id in self._running:
            await interaction.response.send_message(
                "A merge is already running for this server — wait for it to finish.",
                ephemeral=True,
            )
            return
        self._running.add(guild_id)
        try:
            await self._run_merge(interaction, guild_id, dev)
        finally:
            self._running.discard(guild_id)

    async def _run_merge(self, interaction: discord.Interaction, guild_id: int, requested: str | None):
        age = (discord.utils.utcnow() - interaction.created_at).total_seconds()
        logger.info("/merge invoked by %s; interaction age at handler entry: %.2fs",
                    interaction.user, age)
        config = load_merge_config(guild_id)
        try:
            repo, dev = self._resolve_target(guild_id, config, requested)
        except MergeConfigError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        started = f"🧙 Merge run started for `{repo}` → `{dev}`…"
        try:
            await interaction.response.send_message(started)
            status_msg = await interaction.original_response()
        except discord.HTTPException:
            # Interaction token already expired (slow delivery/ack). Discord
            # shows "did not respond", but the merge itself must still run —
            # fall back to plain channel messages for progress and results.
            logger.warning("Interaction ack failed; falling back to channel messages")
            status_msg = await interaction.channel.send(started)

        await self._execute(guild_id, config, repo, dev, interaction.channel, status_msg)

    async def _execute(
        self,
        guild_id: int,
        config: dict,
        repo: str,
        dev: str,
        channel: discord.abc.Messageable,
        status_msg: discord.Message,
    ) -> MergeReport:
        """Shared merge body for slash-command and scheduled runs."""
        url, token = repo_credentials(guild_id, repo)
        engine = MergeEngine(repo_workspace(guild_id, repo), url, token)
        last_edit = 0.0

        async def progress(text: str):
            nonlocal last_edit
            # Throttled so a long branch list can't stall the run on rate limits.
            if time.monotonic() - last_edit < 2.0:
                return
            last_edit = time.monotonic()
            try:
                await status_msg.edit(content=f"🧙 {text}")
            except discord.HTTPException:
                pass

        report = await engine.run(dev, config["sets"][dev]["branches"], progress)

        content, embed = self._build_report_message(config, repo, report)
        await self._deliver_report(channel, status_msg, content, embed)
        return report

    @staticmethod
    async def _deliver_report(
        channel: discord.abc.Messageable,
        status_msg: discord.Message,
        content: str,
        embed: discord.Embed,
    ):
        if content:
            # Mentions added via edit don't notify — a ping needs a fresh
            # message. Send it BEFORE deleting the status message so a failed
            # send can never lose the conflict report.
            try:
                await channel.send(
                    content=content, embed=embed,
                    allowed_mentions=discord.AllowedMentions(users=True),
                )
            except discord.HTTPException:
                logger.exception("Failed to send merge report with ping")
                try:
                    await status_msg.edit(content=content, embed=embed)
                except discord.HTTPException:
                    logger.exception("Fallback merge report edit failed too")
                return
            try:
                await status_msg.delete()
            except discord.HTTPException:
                pass
        else:
            try:
                await status_msg.edit(content=None, embed=embed)
            except discord.HTTPException:
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    logger.exception("Failed to deliver merge report")

    # ------------------------------------------------------- /mergeconfig ...

    config_group = app_commands.Group(
        name="mergeconfig", description="Configure the /merge command", guild_only=True
    )

    @config_group.command(name="repo", description="Pick which registered repo /merge operates on")
    @app_commands.describe(name="Repo name (from /repo list)")
    @require_permissions()
    async def config_repo(self, interaction: discord.Interaction, name: str):
        name_upper = name.upper()
        if name_upper not in repo_names(interaction.guild_id):
            await interaction.response.send_message(
                f"Repo `{name_upper}` is not registered — add it with `/repo add` first.", ephemeral=True
            )
            return
        config = load_merge_config(interaction.guild_id)
        config["repo"] = name_upper
        save_merge_config(interaction.guild_id, config)
        await interaction.response.send_message(f"/merge now operates on `{name_upper}`.", ephemeral=True)

    @config_group.command(name="create", description="Create a merge set for another shared dev branch")
    @app_commands.describe(dev_branch="Branch this set's user branches are merged into (e.g. patch-development)")
    @require_permissions()
    async def config_create(self, interaction: discord.Interaction, dev_branch: str):
        if not _valid_branch(dev_branch):
            await interaction.response.send_message("That doesn't look like a valid branch name.", ephemeral=True)
            return
        config = load_merge_config(interaction.guild_id)
        try:
            create_set(config, dev_branch)
        except MergeConfigError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        save_merge_config(interaction.guild_id, config)
        if config["default"] == dev_branch:
            note = "It is the default set, so a plain `/merge` runs it. Add its user branches with `/mergeconfig add`."
        else:
            note = (
                f"Add its user branches with `/mergeconfig add` (pick `dev:{dev_branch}`), "
                f"then run it with `/merge dev:{dev_branch}`."
            )
        await interaction.response.send_message(f"Created the `{dev_branch}` merge set. {note}", ephemeral=True)

    @config_group.command(name="rename", description="Change a merge set's dev branch, keeping its user branches and schedule")
    @app_commands.describe(dev="Current dev branch of the set", new_branch="Dev branch the set should use from now on")
    @require_permissions()
    async def config_rename(self, interaction: discord.Interaction, dev: str, new_branch: str):
        if not _valid_branch(new_branch):
            await interaction.response.send_message("That doesn't look like a valid branch name.", ephemeral=True)
            return
        config = load_merge_config(interaction.guild_id)
        try:
            rename_set(config, dev, new_branch)
        except MergeConfigError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        save_merge_config(interaction.guild_id, config)
        # The schedule job is keyed by dev branch, so it has to move too.
        self._unregister_schedule(interaction.guild_id, dev)
        schedule = config["sets"][new_branch]["schedule"]
        if schedule:
            self._register_schedule(interaction.guild_id, new_branch, schedule)
        await interaction.response.send_message(
            f"The `{dev}` merge set now uses `{new_branch}` as its dev branch.", ephemeral=True
        )

    @config_group.command(name="delete", description="Delete a merge set (branches on the remote are not touched)")
    @app_commands.describe(dev="Dev branch of the set to delete")
    @require_permissions()
    async def config_delete(self, interaction: discord.Interaction, dev: str):
        config = load_merge_config(interaction.guild_id)
        try:
            removed = delete_set(config, dev)
        except MergeConfigError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        save_merge_config(interaction.guild_id, config)
        self._unregister_schedule(interaction.guild_id, dev)
        count = len(removed["branches"])
        note = f"{count} user branch{'es' if count != 1 else ''} dropped from the config"
        if removed["schedule"]:
            note += ", daily merge turned off"
        await interaction.response.send_message(
            f"Deleted the `{dev}` merge set — {note}. Nothing was changed on the remote.", ephemeral=True
        )

    @config_group.command(name="default", description="Pick the merge set used when a command doesn't name one")
    @app_commands.describe(dev="Dev branch of the set to make the default")
    @require_permissions()
    async def config_default(self, interaction: discord.Interaction, dev: str):
        config = load_merge_config(interaction.guild_id)
        try:
            set_default(config, dev)
        except MergeConfigError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        save_merge_config(interaction.guild_id, config)
        await interaction.response.send_message(
            f"`{dev}` is now the default set — a plain `/merge` runs it.", ephemeral=True
        )

    @config_group.command(name="add", description="Add a team member's branch to a merge set")
    @app_commands.describe(branch="Branch name to include in /merge", dev=DEV_OPTION)
    @require_permissions()
    async def config_add(self, interaction: discord.Interaction, branch: str, dev: str | None = None):
        if not _valid_branch(branch):
            await interaction.response.send_message("That doesn't look like a valid branch name.", ephemeral=True)
            return
        config = load_merge_config(interaction.guild_id)
        try:
            target = resolve_set(config, dev)
            add_branch(config, target, branch)
        except MergeConfigError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        save_merge_config(interaction.guild_id, config)
        await interaction.response.send_message(f"Added `{branch}` to the `{target}` merge list.", ephemeral=True)

    @config_group.command(name="remove", description="Remove a branch from a merge set")
    @app_commands.describe(branch="Branch name to remove", dev=DEV_OPTION)
    @require_permissions()
    async def config_remove(self, interaction: discord.Interaction, branch: str, dev: str | None = None):
        config = load_merge_config(interaction.guild_id)
        try:
            target = resolve_set(config, dev)
            remove_branch(config, target, branch)
        except MergeConfigError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        save_merge_config(interaction.guild_id, config)
        await interaction.response.send_message(f"Removed `{branch}` from the `{target}` merge list.", ephemeral=True)

    @config_group.command(name="release", description="Pick the release branch /patchnotes looks at by default")
    @app_commands.describe(branch="Branch your version tags (v1.0.3, …) are made on")
    @require_permissions()
    async def config_release(self, interaction: discord.Interaction, branch: str):
        if not _valid_branch(branch):
            await interaction.response.send_message("That doesn't look like a valid branch name.", ephemeral=True)
            return
        config = load_merge_config(interaction.guild_id)
        config["release_branch"] = branch
        save_merge_config(interaction.guild_id, config)
        await interaction.response.send_message(
            f"`/patchnotes` now looks at `{branch}` by default.", ephemeral=True
        )

    @config_group.command(name="ping", description="Who gets pinged when a merge conflict needs resolving")
    @app_commands.describe(user="Team member to ping on conflicts")
    @require_permissions()
    async def config_ping(self, interaction: discord.Interaction, user: discord.Member):
        config = load_merge_config(interaction.guild_id)
        config["ping_user_id"] = user.id
        save_merge_config(interaction.guild_id, config)
        await interaction.response.send_message(f"{user.mention} will be pinged on merge conflicts.", ephemeral=True)

    @config_group.command(name="schedule", description="Run a merge set automatically every day at a set time")
    @app_commands.describe(
        time="24-hour time in the bot host's timezone, e.g. 03:30",
        channel="Channel to post the merge results in",
        dev=DEV_OPTION,
    )
    @require_permissions()
    async def config_schedule(
        self,
        interaction: discord.Interaction,
        time: str,
        channel: discord.TextChannel,
        dev: str | None = None,
    ):
        match = TIME_PATTERN.match(time.strip())
        if not match:
            await interaction.response.send_message(
                "Time must be 24-hour `HH:MM`, e.g. `03:30` or `18:00`.", ephemeral=True
            )
            return
        normalized = f"{int(match[1]):02d}:{int(match[2]):02d}"
        config = load_merge_config(interaction.guild_id)
        try:
            target = resolve_set(config, dev)
        except MergeConfigError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        schedule = {"time": normalized, "channel_id": channel.id}
        config["sets"][target]["schedule"] = schedule
        save_merge_config(interaction.guild_id, config)
        self._register_schedule(interaction.guild_id, target, schedule)
        await interaction.response.send_message(
            f"Daily merge of `{target}` scheduled for `{normalized}` (bot-host time) in {channel.mention}.",
            ephemeral=True,
        )

    @config_group.command(name="unschedule", description="Turn off a merge set's daily automatic merge")
    @app_commands.describe(dev=DEV_OPTION)
    @require_permissions()
    async def config_unschedule(self, interaction: discord.Interaction, dev: str | None = None):
        config = load_merge_config(interaction.guild_id)
        try:
            target = resolve_set(config, dev)
        except MergeConfigError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return
        if not config["sets"][target]["schedule"]:
            await interaction.response.send_message(
                f"No daily merge is scheduled for `{target}`.", ephemeral=True
            )
            return
        config["sets"][target]["schedule"] = None
        save_merge_config(interaction.guild_id, config)
        self._unregister_schedule(interaction.guild_id, target)
        await interaction.response.send_message(
            f"Daily automatic merge of `{target}` turned off.", ephemeral=True
        )

    @config_group.command(name="show", description="Show the current merge configuration")
    @require_permissions()
    async def config_show(self, interaction: discord.Interaction):
        config = load_merge_config(interaction.guild_id)
        ping_id = config["ping_user_id"] or get_admin_user_id()

        def fmt(value):
            return f"`{value}`" if value else "_not set_"

        ping_str = f"<@{ping_id}>" if ping_id else "_not set_"
        lines = [
            f"**Repo:** {fmt(config['repo'])}",
            f"**Conflict ping:** {ping_str}",
            f"**Release branch:** {fmt(config['release_branch'])}",
        ]
        if not config["sets"]:
            lines += ["", "_No merge sets yet — create one with `/mergeconfig create`._"]
        for dev, merge_set in config["sets"].items():
            branch_list = ", ".join(f"`{b}`" for b in merge_set["branches"]) or "_none_"
            schedule = merge_set["schedule"]
            schedule_str = (
                f"daily at `{schedule['time']}` (bot-host time) in <#{schedule['channel_id']}>"
                if schedule else "_off_"
            )
            lines += [
                "",
                f"**Dev branch:** `{dev}`" + (" · default set" if dev == config["default"] else ""),
                f"**User branches:** {branch_list}",
                f"**Daily merge:** {schedule_str}",
            ]
        description = "\n".join(lines)
        if len(description) > EMBED_DESC_LIMIT:
            description = description[: EMBED_DESC_LIMIT - 1] + "…"
        embed = discord.Embed(title="Merge configuration", description=description, color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------ autocomplete

    @config_repo.autocomplete("name")
    async def repo_autocomplete(self, interaction: discord.Interaction, current: str):
        # Autocomplete bypasses command checks — gate it explicitly or any
        # member could enumerate repo names.
        if check_permissions(interaction, "mergeconfig repo"):
            return []
        return [
            app_commands.Choice(name=n, value=n)
            for n in repo_names(interaction.guild_id)
            if current.upper() in n
        ][:25]

    @merge.autocomplete("dev")
    @roundup_show.autocomplete("dev")
    @roundup_post.autocomplete("dev")
    @roundup_skip.autocomplete("dev")
    @config_rename.autocomplete("dev")
    @config_delete.autocomplete("dev")
    @config_default.autocomplete("dev")
    @config_add.autocomplete("dev")
    @config_remove.autocomplete("dev")
    @config_schedule.autocomplete("dev")
    @config_unschedule.autocomplete("dev")
    async def dev_autocomplete(self, interaction: discord.Interaction, current: str):
        """Suggest the guild's merge sets, by dev branch."""
        if not autocomplete_allowed(interaction):
            return []
        config = load_merge_config(interaction.guild_id)
        return [
            app_commands.Choice(name=f"{d} (default)" if d == config["default"] else d, value=d)
            for d in config["sets"]
            if current.lower() in d.lower()
        ][:25]

    @config_remove.autocomplete("branch")
    async def remove_autocomplete(self, interaction: discord.Interaction, current: str):
        if not autocomplete_allowed(interaction):
            return []
        config = load_merge_config(interaction.guild_id)
        # Follow the set already picked in `dev`, else the default set.
        dev = interaction.namespace.dev or config["default"]
        branches = config["sets"].get(dev, {}).get("branches", [])
        return [
            app_commands.Choice(name=b, value=b)
            for b in branches
            if current.lower() in b.lower()
        ][:25]

    @config_add.autocomplete("branch")
    @config_create.autocomplete("dev_branch")
    @config_rename.autocomplete("new_branch")
    async def free_branch_autocomplete(self, interaction: discord.Interaction, current: str):
        """Suggest remote branches no merge set uses yet, from the cloned workspace if it exists."""
        if not autocomplete_allowed(interaction):
            return []
        config = load_merge_config(interaction.guild_id)
        return [
            app_commands.Choice(name=b, value=b)
            for b in await self._remote_branches(interaction.guild_id, config)
            if branch_owner(config, b) is None and current.lower() in b.lower()
        ][:25]

    @patchnotes.autocomplete("branch")
    @config_release.autocomplete("branch")
    async def remote_branch_autocomplete(self, interaction: discord.Interaction, current: str):
        """Suggest any remote branch, from the cloned workspace if it exists."""
        if not autocomplete_allowed(interaction):
            return []
        config = load_merge_config(interaction.guild_id)
        return [
            app_commands.Choice(name=b, value=b)
            for b in await self._remote_branches(interaction.guild_id, config)
            if current.lower() in b.lower()
        ][:25]

    @patchnotes.autocomplete("version")
    async def version_autocomplete(self, interaction: discord.Interaction, current: str):
        """Suggest version tags on the branch already picked, else the release branch — newest first."""
        if not autocomplete_allowed(interaction):
            return []
        config = load_merge_config(interaction.guild_id)
        branch = interaction.namespace.branch or config["release_branch"]
        workspace = self._cloned_workspace(interaction.guild_id, config)
        if workspace is None or not branch or not _valid_branch(branch):
            return []
        try:
            tags = await version_tags(
                workspace, build_git_env(), f"refs/remotes/origin/{branch}", timeout=2
            )
        except Exception:
            return []
        wanted = current.strip().lower().lstrip("v")
        return [app_commands.Choice(name=t, value=t) for t in tags if wanted in t][:25]

    # ----------------------------------------------------------------- helpers

    def _cloned_workspace(self, guild_id: int, config: dict):
        """The workspace of the repo merge commands act on, or None if there is no clone to ask."""
        try:
            repo = self._resolve_repo(guild_id, config)
        except MergeConfigError:
            return None
        workspace = repo_workspace(guild_id, repo)
        return workspace if (workspace / ".git").exists() else None

    async def _remote_branches(self, guild_id: int, config: dict) -> list[str]:
        """Remote branch names as of the last fetch; [] without a clone. For autocomplete."""
        workspace = self._cloned_workspace(guild_id, config)
        if workspace is None:
            return []
        try:
            _, out, _ = await git(
                "for-each-ref", "--format=%(refname:strip=3)", "refs/remotes/origin",
                cwd=workspace, env=build_git_env(), timeout=2,
            )
        except Exception:
            return []
        return [b for b in out.strip().splitlines() if b and b != "HEAD"]

    def _resolve_repo(self, guild_id: int, config: dict) -> str:
        """The repo merge commands act on; MergeConfigError names the fix otherwise."""
        names = repo_names(guild_id)
        if not names:
            raise MergeConfigError("No repo registered yet — add one with `/repo add`.")
        if config["repo"] and config["repo"] not in names:
            raise MergeConfigError(
                f"Configured repo `{config['repo']}` is no longer registered — fix it with `/mergeconfig repo`."
            )
        if not config["repo"] and len(names) > 1:
            raise MergeConfigError("Several repos are registered — pick one with `/mergeconfig repo`.")
        return config["repo"] or names[0]

    def _resolve_target(self, guild_id: int, config: dict, requested: str | None) -> tuple[str, str]:
        """(repo, dev branch) a run should act on; MergeConfigError names the fix otherwise."""
        repo = self._resolve_repo(guild_id, config)
        dev = resolve_set(config, requested)
        if not config["sets"][dev]["branches"]:
            raise MergeConfigError(
                f"No user branches configured for `{dev}` — add them with `/mergeconfig add`."
            )
        return repo, dev

    def _build_report_message(
        self, config: dict, repo: str, report: MergeReport
    ) -> tuple[str, discord.Embed | None]:
        if report.conflict is not None:
            return self._conflict_message(config, repo, report)

        if not report.ok:
            embed = discord.Embed(
                title="❌ Merge failed",
                description=report.error or "Unknown error.",
                color=0xE74C3C,
            )
            embed.set_footer(text=f"{repo} · dev branch: {report.dev_branch}")
            return "", embed

        lines = []
        for r in report.results:
            line = STATUS_LINES[r.status].format(
                branch=r.branch, commits=r.commits, s="s" if r.commits != 1 else ""
            )
            lines.append(line + SYNC_NOTES.get(r.sync, ""))

        if report.dev_new_commits:
            summary = f"`{report.dev_branch}` gained **{report.dev_new_commits}** new commit{'s' if report.dev_new_commits != 1 else ''}. Everyone's branch now matches `{report.dev_branch}` — pull before you keep working!"
        else:
            summary = f"Nothing new to merge — `{report.dev_branch}` already had everyone's work. Branches were synced back up where needed."

        description = "\n".join(lines) + "\n\n" + summary
        if len(description) > EMBED_DESC_LIMIT:
            description = description[: EMBED_DESC_LIMIT - 1] + "…"
        embed = discord.Embed(
            title="✅ Merge complete",
            description=description,
            color=0x2ECC71,
        )
        embed.set_footer(text=f"{repo} · dev branch: {report.dev_branch}")
        return "", embed

    def _conflict_message(
        self, config: dict, repo: str, report: MergeReport
    ) -> tuple[str, discord.Embed]:
        conflict: BranchResult = report.conflict
        files = conflict.conflict_files

        # Dev doesn't contain the branches that merged cleanly this run (nothing
        # was pushed), so the resolver must replay them before the conflicting
        # merge or the conflict won't reproduce / will bounce to the next run.
        prior = [r.branch for r in report.results if r.status == "merged"]
        # Only the default set runs without being named.
        set_arg = "" if report.dev_branch == config["default"] else f" dev:{report.dev_branch}"

        def build(max_files: int) -> str:
            shown = "\n".join(f"• `{f}`" for f in files[:max_files])
            if len(files) > max_files:
                shown += f"\n… and {len(files) - max_files} more"
            prior_merges = "".join(f"git merge origin/{b}\n" for b in prior)
            instructions = (
                f"git checkout {report.dev_branch}\n"
                f"git pull\n"
                f"{prior_merges}"
                f"git merge origin/{conflict.branch}   # <- conflict happens here\n"
                f"# fix the conflicted files, then:\n"
                f"git add -A\n"
                f"git commit\n"
                f"git push"
            )
            return (
                "The run was stopped and **nothing was pushed** — all branches are untouched.\n\n"
                f"**Conflicting files:**\n{shown}\n\n"
                f"**To resolve, in your own clone:**\n```sh\n{instructions}\n```\n"
                f"Then run `/merge{set_arg}` again — and `/roundup post{set_arg}` if the team "
                "shouldn't wait for the daily round-up."
            )

        description = build(20)
        if len(description) > EMBED_DESC_LIMIT:
            description = build(5)
        if len(description) > EMBED_DESC_LIMIT:
            description = description[: EMBED_DESC_LIMIT - 1] + "…"

        embed = discord.Embed(
            title=f"⛔ Conflict merging `{conflict.branch}` into `{report.dev_branch}`",
            description=description,
            color=0xE74C3C,
        )
        embed.set_footer(text=f"{repo} · dev branch: {report.dev_branch}")

        ping_id = config["ping_user_id"] or get_admin_user_id()
        content = f"<@{ping_id}> a merge conflict needs your attention." if ping_id else ""
        return content, embed


async def setup(bot: commands.Bot):
    await bot.add_cog(MergeCog(bot))
