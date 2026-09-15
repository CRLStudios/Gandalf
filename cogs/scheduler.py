import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from config import SCHEDULES_DIR, WORKSPACES_DIR
from utils.guild_env import load_guild_env
from utils.runner import run_script, resolve_script
from utils.permissions import autocomplete_allowed, require_permissions
from cogs.scripts import ScriptsCog


class SchedulerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.scheduler = AsyncIOScheduler()

    async def cog_load(self):
        self._load_all_jobs()
        self.scheduler.start()

    async def cog_unload(self):
        self.scheduler.shutdown(wait=False)

    # --- Slash command group ---

    schedule_group = app_commands.Group(name="schedule", description="Manage scheduled scripts")

    @schedule_group.command(name="add", description="Schedule a script with cron")
    @app_commands.describe(
        script="Script name",
        cron="Cron expression (e.g. */5 * * * *)",
        channel="Channel to post results in",
        args="Space-separated arguments",
    )
    @require_permissions()
    async def schedule_add(
        self,
        interaction: discord.Interaction,
        script: str,
        cron: str,
        channel: discord.TextChannel,
        args: str | None = None,
    ):
        # Validate script exists
        path = resolve_script(interaction.guild_id, script)
        if path is None:
            await interaction.response.send_message(f"Script `{script}` not found.", ephemeral=True)
            return

        # Validate cron
        try:
            trigger = CronTrigger.from_crontab(cron)
        except ValueError as e:
            await interaction.response.send_message(f"Invalid cron: {e}", ephemeral=True)
            return

        job_id = uuid.uuid4().hex[:8]
        job_data = {
            "id": job_id,
            "script": script,
            "args": args or "",
            "channel_id": channel.id,
            "cron": cron,
            "added_by": interaction.user.id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        self._save_job(interaction.guild_id, job_data)
        self._register_job(interaction.guild_id, job_data, trigger)

        await interaction.response.send_message(
            f"Scheduled `{script}` with cron `{cron}` in {channel.mention} (ID: `{job_id}`)",
            ephemeral=True,
        )

    @schedule_group.command(name="remove", description="Remove a scheduled job")
    @app_commands.describe(job_id="Job ID to remove")
    @require_permissions()
    async def schedule_remove(self, interaction: discord.Interaction, job_id: str):
        guild_id = interaction.guild_id
        jobs = self._load_jobs(guild_id)
        job = next((j for j in jobs if j["id"] == job_id), None)

        if job is None:
            await interaction.response.send_message(f"Job `{job_id}` not found.", ephemeral=True)
            return

        jobs.remove(job)
        self._write_jobs(guild_id, jobs)

        scheduler_id = f"{guild_id}_{job_id}"
        if self.scheduler.get_job(scheduler_id):
            self.scheduler.remove_job(scheduler_id)

        await interaction.response.send_message(f"Removed job `{job_id}`.", ephemeral=True)

    @schedule_remove.autocomplete("job_id")
    async def remove_autocomplete(self, interaction: discord.Interaction, current: str):
        # Autocomplete bypasses command checks — gate it explicitly or any
        # member could enumerate job ids and script names.
        if not autocomplete_allowed(interaction):
            return []
        jobs = self._load_jobs(interaction.guild_id)
        return [
            app_commands.Choice(name=f"{j['id']} — {j['script']} ({j['cron']})", value=j["id"])
            for j in jobs
            if current.lower() in j["id"].lower() or current.lower() in j["script"].lower()
        ][:25]

    @schedule_group.command(name="list", description="List scheduled jobs")
    @require_permissions()
    async def schedule_list(self, interaction: discord.Interaction):
        jobs = self._load_jobs(interaction.guild_id)
        if not jobs:
            await interaction.response.send_message("No scheduled jobs.", ephemeral=True)
            return

        lines = []
        for j in jobs:
            lines.append(f"`{j['id']}` — `{j['script']}` | `{j['cron']}` | <#{j['channel_id']}>")

        embed = discord.Embed(title="Scheduled Jobs", description="\n".join(lines), color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- Job execution callback ---

    async def _execute_job(self, guild_id: int, job_data: dict):
        path = resolve_script(guild_id, job_data["script"])
        if path is None:
            return

        arg_list = job_data["args"].split() if job_data["args"] else []
        # Same execution context as /run: guild workspace cwd + guild env.
        workspace = WORKSPACES_DIR / str(guild_id)
        workspace.mkdir(parents=True, exist_ok=True)
        guild_env = load_guild_env(guild_id)
        returncode, stdout, stderr = await run_script(path, arg_list, cwd=workspace, env=guild_env)

        channel = self.bot.get_channel(job_data["channel_id"])
        if channel is None:
            return

        embed = ScriptsCog._build_embed(job_data["script"], returncode, stdout, stderr)
        embed.set_author(name="Scheduled Run")
        await channel.send(embed=embed)

    # --- Internal helpers ---

    def _jobs_path(self, guild_id: int) -> Path:
        return SCHEDULES_DIR / f"{guild_id}.json"

    def _load_jobs(self, guild_id: int) -> list[dict]:
        path = self._jobs_path(guild_id)
        if not path.exists():
            return []
        data = json.loads(path.read_text())
        return data.get("jobs", [])

    def _write_jobs(self, guild_id: int, jobs: list[dict]):
        path = self._jobs_path(guild_id)
        path.write_text(json.dumps({"jobs": jobs}, indent=2))

    def _save_job(self, guild_id: int, job_data: dict):
        jobs = self._load_jobs(guild_id)
        jobs.append(job_data)
        self._write_jobs(guild_id, jobs)

    def _register_job(self, guild_id: int, job_data: dict, trigger: CronTrigger | None = None):
        if trigger is None:
            trigger = CronTrigger.from_crontab(job_data["cron"])
        scheduler_id = f"{guild_id}_{job_data['id']}"
        self.scheduler.add_job(
            self._execute_job,
            trigger=trigger,
            id=scheduler_id,
            args=[guild_id, job_data],
            replace_existing=True,
        )

    def _load_all_jobs(self):
        if not SCHEDULES_DIR.is_dir():
            return
        for file in SCHEDULES_DIR.glob("*.json"):
            try:
                guild_id = int(file.stem)
            except ValueError:
                continue
            for job_data in self._load_jobs(guild_id):
                try:
                    self._register_job(guild_id, job_data)
                except Exception:
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(SchedulerCog(bot))
