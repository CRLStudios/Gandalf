import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_TOKEN, SCRIPTS_DIR, WORKSPACES_DIR
from utils.permissions import get_allowed_guilds

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def setup_hook():
    await bot.load_extension("cogs.scripts")
    await bot.load_extension("cogs.scheduler")
    await bot.load_extension("cogs.shortcuts")
    await bot.tree.sync()


@bot.event
async def on_ready():
    allowed = get_allowed_guilds()
    if not allowed:
        for guild in bot.guilds:
            guild_dir = SCRIPTS_DIR / str(guild.id)
            guild_dir.mkdir(parents=True, exist_ok=True)
            (guild_dir / f"{guild.name}.txt").touch()
            (WORKSPACES_DIR / str(guild.id)).mkdir(parents=True, exist_ok=True)
        return
    for guild in bot.guilds:
        if guild.id not in allowed:
            logger.warning(f"Leaving unauthorized guild: {guild.name} ({guild.id})")
            await guild.leave()
        else:
            guild_dir = SCRIPTS_DIR / str(guild.id)
            guild_dir.mkdir(parents=True, exist_ok=True)
            (guild_dir / f"{guild.name}.txt").touch()
            (WORKSPACES_DIR / str(guild.id)).mkdir(parents=True, exist_ok=True)


@bot.event
async def on_guild_join(guild: discord.Guild):
    allowed = get_allowed_guilds()
    if allowed and guild.id not in allowed:
        logger.warning(f"Joined unauthorized guild, leaving: {guild.name} ({guild.id})")
        await guild.leave()
        return
    guild_dir = SCRIPTS_DIR / str(guild.id)
    guild_dir.mkdir(parents=True, exist_ok=True)
    (guild_dir / f"{guild.name}.txt").touch()
    (WORKSPACES_DIR / str(guild.id)).mkdir(parents=True, exist_ok=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        msg = str(error) or "Permission denied."
    else:
        msg = f"Error: {error}"

    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    bot.run(BOT_TOKEN)
