import asyncio
import re

import discord
from discord import app_commands
from discord.ext import commands

from utils.guild_env import set_guild_env, remove_guild_env
from utils.permissions import autocomplete_allowed, require_permissions
from utils.repos import repo_names, repo_workspace

NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


class ReposCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    repo_group = app_commands.Group(name="repo", description="Manage guild repositories")

    def _repo_names(self, guild_id: int) -> list[str]:
        return repo_names(guild_id)

    @repo_group.command(name="add", description="Add a repository")
    @app_commands.describe(name="Repo name (alphanumeric + _)", url="Git clone URL", token="Optional access token")
    @require_permissions()
    async def repo_add(self, interaction: discord.Interaction, name: str, url: str, token: str | None = None):
        name_upper = name.upper()
        if not NAME_PATTERN.match(name):
            await interaction.response.send_message("Name must match `[A-Za-z0-9_]+`.", ephemeral=True)
            return
        if url.startswith("-"):
            await interaction.response.send_message("Invalid URL.", ephemeral=True)
            return
        set_guild_env(interaction.guild_id, f"REPO_{name_upper}_URL", url)
        if token:
            set_guild_env(interaction.guild_id, f"REPO_{name_upper}_TOKEN", token)
        await interaction.response.send_message(f"Repo `{name_upper}` added.", ephemeral=True)

    @repo_group.command(name="remove", description="Remove a repository")
    @app_commands.describe(name="Repo name to remove")
    @require_permissions()
    async def repo_remove(self, interaction: discord.Interaction, name: str):
        name_upper = name.upper()
        removed_url = remove_guild_env(interaction.guild_id, f"REPO_{name_upper}_URL")
        remove_guild_env(interaction.guild_id, f"REPO_{name_upper}_TOKEN")
        if removed_url:
            await interaction.response.send_message(f"Repo `{name_upper}` removed.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Repo `{name_upper}` not found.", ephemeral=True)

    @repo_group.command(name="list", description="List repositories")
    @require_permissions()
    async def repo_list(self, interaction: discord.Interaction):
        names = self._repo_names(interaction.guild_id)
        if not names:
            await interaction.response.send_message("No repos configured.", ephemeral=True)
            return
        listing = "\n".join(f"`{n}`" for n in names)
        await interaction.response.send_message(listing, ephemeral=True)

    @repo_group.command(name="status", description="Show git status of a repo")
    @app_commands.describe(name="Repo name")
    @require_permissions()
    async def repo_status(self, interaction: discord.Interaction, name: str):
        if not NAME_PATTERN.match(name):
            await interaction.response.send_message("Name must match `[A-Za-z0-9_]+`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        name_upper = name.upper()
        repo_dir = repo_workspace(interaction.guild_id, name_upper)

        if not repo_dir.is_dir():
            await interaction.followup.send(f"`{name_upper}` not cloned.", ephemeral=True)
            return

        try:
            status_proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo_dir), "status", "--short",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            branch_proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(repo_dir), "branch", "--show-current",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            s_out, _ = await status_proc.communicate()
            b_out, _ = await branch_proc.communicate()

            branch = b_out.decode().strip() or "unknown"
            status = s_out.decode().strip() or "clean"
            msg = f"**{name_upper}** on `{branch}`\n```\n{status[:1500]}\n```"
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @repo_remove.autocomplete("name")
    @repo_status.autocomplete("name")
    async def name_autocomplete(self, interaction: discord.Interaction, current: str):
        if not autocomplete_allowed(interaction):
            return []
        names = self._repo_names(interaction.guild_id)
        return [
            app_commands.Choice(name=n, value=n)
            for n in names
            if current.upper() in n
        ][:25]


async def setup(bot: commands.Bot):
    await bot.add_cog(ReposCog(bot))
