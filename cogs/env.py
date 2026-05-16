import re

import discord
from discord import app_commands
from discord.ext import commands

from utils.guild_env import set_guild_env, remove_guild_env, list_guild_env_keys
from utils.permissions import require_permissions

KEY_PATTERN = re.compile(r"^[A-Z0-9_]+$")


class EnvCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    env_group = app_commands.Group(name="env", description="Manage guild environment variables")

    @env_group.command(name="set", description="Set an environment variable")
    @app_commands.describe(key="Variable name (A-Z, 0-9, _)", value="Variable value")
    @require_permissions()
    async def env_set(self, interaction: discord.Interaction, key: str, value: str):
        key = key.upper()
        if key.startswith("REPO_"):
            await interaction.response.send_message("Use `/repo add` to manage repo vars.", ephemeral=True)
            return
        if not KEY_PATTERN.match(key):
            await interaction.response.send_message("Key must match `[A-Z0-9_]+`.", ephemeral=True)
            return
        set_guild_env(interaction.guild_id, key, value)
        await interaction.response.send_message(f"`{key}` set.", ephemeral=True)

    @env_group.command(name="remove", description="Remove an environment variable")
    @app_commands.describe(key="Variable name to remove")
    @require_permissions()
    async def env_remove(self, interaction: discord.Interaction, key: str):
        key = key.upper()
        if remove_guild_env(interaction.guild_id, key):
            await interaction.response.send_message(f"`{key}` removed.", ephemeral=True)
        else:
            await interaction.response.send_message(f"`{key}` not found.", ephemeral=True)

    @env_group.command(name="list", description="List environment variable keys")
    @require_permissions()
    async def env_list(self, interaction: discord.Interaction):
        keys = list_guild_env_keys(interaction.guild_id)
        if not keys:
            await interaction.response.send_message("No env vars set.", ephemeral=True)
            return
        listing = "\n".join(f"`{k}`" for k in sorted(keys))
        await interaction.response.send_message(listing, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(EnvCog(bot))
