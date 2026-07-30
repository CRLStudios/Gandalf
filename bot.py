import asyncio
import logging
import signal

import discord
from discord import app_commands
from discord.ext import commands

from config import BOT_TOKEN, SCRIPTS_DIR, WORKSPACES_DIR
from utils.permissions import get_allowed_guilds

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def setup_hook():
    await bot.load_extension("cogs.scripts")
    await bot.load_extension("cogs.scheduler")
    await bot.load_extension("cogs.shortcuts")
    await bot.load_extension("cogs.env")
    await bot.load_extension("cogs.repos")
    await bot.load_extension("cogs.merge")
    await bot.tree.sync()


@bot.event
async def on_ready():
    logger.info("Bot ready: %s (ID %s)", bot.user, bot.user.id)
    allowed = get_allowed_guilds()
    if not allowed:
        for guild in bot.guilds:
            guild_dir = SCRIPTS_DIR / str(guild.id)
            guild_dir.mkdir(parents=True, exist_ok=True)
            (guild_dir / f"{guild.name}.txt").touch()
            (WORKSPACES_DIR / str(guild.id)).mkdir(parents=True, exist_ok=True)
            #bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
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
            #bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)


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
    # copy_global_to would duplicate every global command in the guild list
    # (same bug fixed in on_ready earlier) — guild sync alone is correct here.
    await bot.tree.sync(guild=guild)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    command = interaction.command.qualified_name if interaction.command else "?"
    if isinstance(error, app_commands.CheckFailure):
        msg = str(error) or "Permission denied."
        logger.warning(
            "Denied /%s for %s (ID %s): %s",
            command, interaction.user, interaction.user.id, msg,
        )
    else:
        msg = f"Error: {error}"
        logger.error("Command /%s failed for %s: %r", command, interaction.user, error)

    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        # Expired interaction token — nothing to deliver the error to.
        logger.warning("Could not deliver error message for /%s (interaction expired)", command)


async def main():
    async with bot:
        await bot.start(BOT_TOKEN)


if __name__ == "__main__":
    loop = asyncio.new_event_loop()

    async def _close():
        await bot.close()
        logger.info("bot.close() completed")

    def _shutdown():
        logger.info("Shutdown signal received, calling bot.close()")
        loop.create_task(_close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
