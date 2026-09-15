import logging
import re
import time

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands
from discord.ext import commands

from utils.gitops import GitError, build_git_env, git
from utils.merge_config import all_guild_ids, load_merge_config, save_merge_config
from utils.merge_engine import BranchResult, MergeEngine, MergeReport
from utils.permissions import check_permissions, get_admin_user_id, require_permissions
from utils.repos import repo_credentials, repo_names, repo_workspace
from utils.roundup import collect_roundup, format_roundup_pages

logger = logging.getLogger(__name__)

BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

EMBED_DESC_LIMIT = 4096

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
            schedule = load_merge_config(guild_id).get("schedule")
            if schedule:
                try:
                    self._register_schedule(guild_id, schedule)
                except Exception:
                    logger.exception("Could not register merge schedule for guild %s", guild_id)
        self.scheduler.start()

    async def cog_unload(self):
        self.scheduler.shutdown(wait=False)

    # --------------------------------------------------------------- scheduling

    def _job_id(self, guild_id: int) -> str:
        return f"merge_{guild_id}"

    def _register_schedule(self, guild_id: int, schedule: dict):
        hour, minute = (int(p) for p in schedule["time"].split(":"))
        self.scheduler.add_job(
            self._scheduled_merge,
            CronTrigger(hour=hour, minute=minute),
            id=self._job_id(guild_id),
            args=[guild_id],
            replace_existing=True,
        )

    def _unregister_schedule(self, guild_id: int):
        if self.scheduler.get_job(self._job_id(guild_id)):
            self.scheduler.remove_job(self._job_id(guild_id))

    async def _scheduled_merge(self, guild_id: int):
        config = load_merge_config(guild_id)
        schedule = config.get("schedule")
        if not schedule:
            # Unscheduled after the job was registered — clean up defensively.
            self._unregister_schedule(guild_id)
            return

        channel = self.bot.get_channel(schedule["channel_id"])
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(schedule["channel_id"])
            except discord.HTTPException:
                logger.warning(
                    "Scheduled merge for guild %s: channel %s unavailable",
                    guild_id, schedule["channel_id"],
                )
                return

        if guild_id in self._running:
            await channel.send("⏰ Scheduled merge skipped — a merge is already running.")
            return
        self._running.add(guild_id)
        try:
            error = self._config_error(guild_id, config)
            if error:
                await channel.send(f"⏰ Scheduled merge skipped: {error}")
                return
            repo = config["repo"] or repo_names(guild_id)[0]
            logger.info("Scheduled merge starting for guild %s (repo %s)", guild_id, repo)
            status_msg = await channel.send(f"⏰ Daily merge run started for `{repo}`…")
            report = await self._execute(guild_id, config, repo, channel, status_msg)
            await self._post_roundup(guild_id, repo, report, channel)
        except Exception:
            logger.exception("Scheduled merge failed for guild %s", guild_id)
        finally:
            self._running.discard(guild_id)

    # ---------------------------------------------------------------- round-up

    async def _post_roundup(
        self,
        guild_id: int,
        repo: str,
        report: MergeReport,
        channel: discord.abc.Messageable,
    ):
        """After a scheduled run: post the tagged-changes digest, if any.

        The baseline advances only when a round-up actually posts, so
        commits land in exactly one report even across manual merges,
        empty days, and failed sends.
        """
        if not report.ok or report.dev_head is None:
            return
        config = load_merge_config(guild_id)
        state = config.get("roundup")
        if not state or state.get("repo") != repo or not state.get("baseline"):
            # First run (or the repo was re-pointed): record silently.
            self._save_baseline(guild_id, repo, report.dev_head)
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
            logger.warning("Round-up baseline invalid for guild %s; resetting", guild_id)
            self._save_baseline(guild_id, repo, report.dev_head)
            return

        pages = format_roundup_pages(groups, EMBED_DESC_LIMIT)
        if not pages:
            return  # nothing tagged — silent, baseline stays

        try:
            for embed in self._roundup_embeds(
                "📋 Daily Round-up", pages, f"{repo} · changes since the last round-up"
            ):
                await channel.send(embed=embed)
        except discord.HTTPException:
            # Baseline stays even if some pages made it out: a duplicated
            # page tomorrow beats silently losing the rest of the report.
            logger.exception("Failed to post round-up for guild %s", guild_id)
            return
        self._save_baseline(guild_id, repo, report.dev_head)

    roundup_group = app_commands.Group(
        name="roundup", description="Round-up of tagged changes on the dev branch", guild_only=True
    )

    @roundup_group.command(name="show", description="Preview the pending round-up (only you see it)")
    @require_permissions()
    async def roundup_show(self, interaction: discord.Interaction):
        await self._roundup_command(interaction, "show")

    @roundup_group.command(name="post", description="Post the pending round-up here for everyone, starting a fresh period")
    @require_permissions()
    async def roundup_post(self, interaction: discord.Interaction):
        await self._roundup_command(interaction, "post")

    @roundup_group.command(name="skip", description="Move the round-up baseline to the current dev head without posting")
    @require_permissions()
    async def roundup_skip(self, interaction: discord.Interaction):
        await self._roundup_command(interaction, "skip")

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

    def _save_baseline(self, guild_id: int, repo: str, tip: str):
        # Reload instead of reusing the caller's config: a /mergeconfig edit
        # made in the meantime must not be clobbered by this save.
        config = load_merge_config(guild_id)
        config["roundup"] = {"repo": repo, "baseline": tip}
        save_merge_config(guild_id, config)

    async def _roundup_command(self, interaction: discord.Interaction, mode: str):
        guild_id = interaction.guild_id
        if guild_id in self._running:
            await interaction.response.send_message(
                "A merge is running for this server — try again in a minute.", ephemeral=True
            )
            return
        self._running.add(guild_id)
        try:
            config = load_merge_config(guild_id)
            error = self._config_error(guild_id, config)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            repo = config["repo"] or repo_names(guild_id)[0]
            state = config.get("roundup")
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
                    f"refs/remotes/origin/{config['dev_branch']}",
                    cwd=workspace, env=env, check=False,
                )
                if rc != 0:
                    await interaction.followup.send(
                        f"Development branch `{config['dev_branch']}` was not found on the remote.",
                        ephemeral=True,
                    )
                    return
                tip = out.strip()

                if mode == "skip":
                    await self._roundup_skip_body(
                        interaction, guild_id, repo, config, state, has_baseline, workspace, env, tip
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
                    "📋 Round-up preview", pages, f"{repo} · posts with the next daily merge"
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
                    "📋 Round-up", pages, f"{repo} · changes since the last round-up"
                ):
                    await interaction.channel.send(embed=embed)
            except discord.HTTPException:
                # Baseline stays even if some pages made it out: a duplicated
                # page next time beats silently losing the rest of the report.
                logger.exception("Failed to post round-up for guild %s", guild_id)
                await interaction.followup.send(
                    "Could not post the full round-up in this channel.", ephemeral=True
                )
                return
            self._save_baseline(guild_id, repo, tip)
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
        config: dict,
        state: dict | None,
        has_baseline: bool,
        workspace,
        env: dict,
        tip: str,
    ):
        if not has_baseline:
            self._save_baseline(guild_id, repo, tip)
            await interaction.followup.send(
                "Round-up baseline started at the current "
                f"`{config['dev_branch']}` head — the next round-up covers changes from now on.",
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
        self._save_baseline(guild_id, repo, tip)
        await interaction.followup.send(
            f"Baseline moved to the current `{config['dev_branch']}` head — {note}", ephemeral=True
        )

    # ------------------------------------------------------------------ /merge

    @app_commands.command(
        name="merge",
        description="Merge everyone's branches into the dev branch and sync them all back up",
    )
    @app_commands.guild_only()
    @require_permissions()
    async def merge(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if guild_id in self._running:
            await interaction.response.send_message(
                "A merge is already running for this server — wait for it to finish.",
                ephemeral=True,
            )
            return
        self._running.add(guild_id)
        try:
            await self._run_merge(interaction, guild_id)
        finally:
            self._running.discard(guild_id)

    async def _run_merge(self, interaction: discord.Interaction, guild_id: int):
        age = (discord.utils.utcnow() - interaction.created_at).total_seconds()
        logger.info("/merge invoked by %s; interaction age at handler entry: %.2fs",
                    interaction.user, age)
        config = load_merge_config(guild_id)
        error = self._config_error(guild_id, config)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return

        repo = config["repo"] or repo_names(guild_id)[0]

        try:
            await interaction.response.send_message(f"🧙 Merge run started for `{repo}`…")
            status_msg = await interaction.original_response()
        except discord.HTTPException:
            # Interaction token already expired (slow delivery/ack). Discord
            # shows "did not respond", but the merge itself must still run —
            # fall back to plain channel messages for progress and results.
            logger.warning("Interaction ack failed; falling back to channel messages")
            status_msg = await interaction.channel.send(
                f"🧙 Merge run started for `{repo}`…"
            )

        await self._execute(guild_id, config, repo, interaction.channel, status_msg)

    async def _execute(
        self,
        guild_id: int,
        config: dict,
        repo: str,
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

        report = await engine.run(config["dev_branch"], config["branches"], progress)

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

    @config_group.command(name="dev", description="Set the shared development branch")
    @app_commands.describe(branch="Branch everyone's work is merged into (e.g. develop)")
    @require_permissions()
    async def config_dev(self, interaction: discord.Interaction, branch: str):
        if not _valid_branch(branch):
            await interaction.response.send_message("That doesn't look like a valid branch name.", ephemeral=True)
            return
        config = load_merge_config(interaction.guild_id)
        config["dev_branch"] = branch
        if branch in config["branches"]:
            config["branches"].remove(branch)
        save_merge_config(interaction.guild_id, config)
        await interaction.response.send_message(f"Development branch set to `{branch}`.", ephemeral=True)

    @config_group.command(name="add", description="Add a team member's branch to the merge list")
    @app_commands.describe(branch="Branch name to include in /merge")
    @require_permissions()
    async def config_add(self, interaction: discord.Interaction, branch: str):
        if not _valid_branch(branch):
            await interaction.response.send_message("That doesn't look like a valid branch name.", ephemeral=True)
            return
        config = load_merge_config(interaction.guild_id)
        if branch == config["dev_branch"]:
            await interaction.response.send_message(
                f"`{branch}` is the development branch — it can't also be a user branch.", ephemeral=True
            )
            return
        if branch in config["branches"]:
            await interaction.response.send_message(f"`{branch}` is already in the merge list.", ephemeral=True)
            return
        config["branches"].append(branch)
        save_merge_config(interaction.guild_id, config)
        await interaction.response.send_message(f"Added `{branch}` to the merge list.", ephemeral=True)

    @config_group.command(name="remove", description="Remove a branch from the merge list")
    @app_commands.describe(branch="Branch name to remove")
    @require_permissions()
    async def config_remove(self, interaction: discord.Interaction, branch: str):
        config = load_merge_config(interaction.guild_id)
        if branch not in config["branches"]:
            await interaction.response.send_message(f"`{branch}` is not in the merge list.", ephemeral=True)
            return
        config["branches"].remove(branch)
        save_merge_config(interaction.guild_id, config)
        await interaction.response.send_message(f"Removed `{branch}` from the merge list.", ephemeral=True)

    @config_group.command(name="ping", description="Who gets pinged when a merge conflict needs resolving")
    @app_commands.describe(user="Team member to ping on conflicts")
    @require_permissions()
    async def config_ping(self, interaction: discord.Interaction, user: discord.Member):
        config = load_merge_config(interaction.guild_id)
        config["ping_user_id"] = user.id
        save_merge_config(interaction.guild_id, config)
        await interaction.response.send_message(f"{user.mention} will be pinged on merge conflicts.", ephemeral=True)

    @config_group.command(name="schedule", description="Run the merge automatically every day at a set time")
    @app_commands.describe(
        time="24-hour time in the bot host's timezone, e.g. 03:30",
        channel="Channel to post the merge results in",
    )
    @require_permissions()
    async def config_schedule(
        self, interaction: discord.Interaction, time: str, channel: discord.TextChannel
    ):
        match = TIME_PATTERN.match(time.strip())
        if not match:
            await interaction.response.send_message(
                "Time must be 24-hour `HH:MM`, e.g. `03:30` or `18:00`.", ephemeral=True
            )
            return
        normalized = f"{int(match[1]):02d}:{int(match[2]):02d}"
        config = load_merge_config(interaction.guild_id)
        config["schedule"] = {"time": normalized, "channel_id": channel.id}
        save_merge_config(interaction.guild_id, config)
        self._register_schedule(interaction.guild_id, config["schedule"])
        await interaction.response.send_message(
            f"Daily merge scheduled for `{normalized}` (bot-host time) in {channel.mention}.",
            ephemeral=True,
        )

    @config_group.command(name="unschedule", description="Turn off the daily automatic merge")
    @require_permissions()
    async def config_unschedule(self, interaction: discord.Interaction):
        config = load_merge_config(interaction.guild_id)
        if not config["schedule"]:
            await interaction.response.send_message("No daily merge is scheduled.", ephemeral=True)
            return
        config["schedule"] = None
        save_merge_config(interaction.guild_id, config)
        self._unregister_schedule(interaction.guild_id)
        await interaction.response.send_message("Daily automatic merge turned off.", ephemeral=True)

    @config_group.command(name="show", description="Show the current merge configuration")
    @require_permissions()
    async def config_show(self, interaction: discord.Interaction):
        config = load_merge_config(interaction.guild_id)
        ping_id = config["ping_user_id"] or get_admin_user_id()

        def fmt(value):
            return f"`{value}`" if value else "_not set_"

        branch_list = ", ".join(f"`{b}`" for b in config["branches"]) or "_none_"
        ping_str = f"<@{ping_id}>" if ping_id else "_not set_"
        schedule = config["schedule"]
        schedule_str = (
            f"daily at `{schedule['time']}` (bot-host time) in <#{schedule['channel_id']}>"
            if schedule else "_off_"
        )
        lines = [
            f"**Repo:** {fmt(config['repo'])}",
            f"**Dev branch:** {fmt(config['dev_branch'])}",
            f"**User branches:** {branch_list}",
            f"**Conflict ping:** {ping_str}",
            f"**Daily merge:** {schedule_str}",
        ]
        embed = discord.Embed(title="Merge configuration", description="\n".join(lines), color=0x5865F2)
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

    @config_remove.autocomplete("branch")
    async def remove_autocomplete(self, interaction: discord.Interaction, current: str):
        if check_permissions(interaction, "mergeconfig remove"):
            return []
        config = load_merge_config(interaction.guild_id)
        return [
            app_commands.Choice(name=b, value=b)
            for b in config["branches"]
            if current.lower() in b.lower()
        ][:25]

    @config_add.autocomplete("branch")
    async def add_autocomplete(self, interaction: discord.Interaction, current: str):
        """Suggest remote branches from the cloned workspace, if it exists."""
        if check_permissions(interaction, "mergeconfig add"):
            return []
        config = load_merge_config(interaction.guild_id)
        repo = config["repo"]
        if not repo:
            return []
        workspace = repo_workspace(interaction.guild_id, repo)
        if not (workspace / ".git").exists():
            return []
        try:
            _, out, _ = await git(
                "for-each-ref", "--format=%(refname:strip=3)", "refs/remotes/origin",
                cwd=workspace, env=build_git_env(), timeout=2,
            )
        except Exception:
            return []
        taken = set(config["branches"]) | {config["dev_branch"], "HEAD"}
        return [
            app_commands.Choice(name=b, value=b)
            for b in out.strip().splitlines()
            if b and b not in taken and current.lower() in b.lower()
        ][:25]

    # ----------------------------------------------------------------- helpers

    def _config_error(self, guild_id: int, config: dict) -> str | None:
        names = repo_names(guild_id)
        if not names:
            return "No repo registered yet — add one with `/repo add`."
        if config["repo"] and config["repo"] not in names:
            return f"Configured repo `{config['repo']}` is no longer registered — fix it with `/mergeconfig repo`."
        if not config["repo"] and len(names) > 1:
            return "Several repos are registered — pick one with `/mergeconfig repo`."
        if not config["dev_branch"]:
            return "No development branch set — set it with `/mergeconfig dev`."
        if not config["branches"]:
            return "No user branches configured — add them with `/mergeconfig add`."
        return None

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
                "Then run `/merge` again — and `/roundup post` if the team "
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
