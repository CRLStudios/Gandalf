import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from config import SHORTCUTS_DIR
from utils.runner import run_script, resolve_script
from cogs.scripts import ScriptsCog

logger = logging.getLogger(__name__)

RESERVED_NAMES = {"run", "list", "schedule", "unschedule", "schedules"}


def _load_shortcuts() -> tuple[dict[str, dict], dict[int, dict[str, dict]]]:
    """Load global and per-guild shortcuts from JSON files."""
    global_shortcuts: dict[str, dict] = {}
    guild_shortcuts: dict[int, dict[str, dict]] = {}

    global_file = SHORTCUTS_DIR / "global.json"
    if global_file.is_file():
        with open(global_file) as f:
            global_shortcuts = json.load(f)

    if SHORTCUTS_DIR.is_dir():
        for path in SHORTCUTS_DIR.iterdir():
            if path.suffix == ".json" and path.stem != "global":
                try:
                    guild_id = int(path.stem)
                except ValueError:
                    continue
                with open(path) as f:
                    guild_shortcuts[guild_id] = json.load(f)

    return global_shortcuts, guild_shortcuts


def _make_command(name: str, conf: dict) -> app_commands.Command:
    """Create an app command that runs the configured script."""
    script_name = conf["script"]
    description = conf.get("description", f"Run {script_name}")

    @app_commands.describe(args="Space-separated arguments", silent="Send result only to you")
    async def callback(interaction: discord.Interaction, args: str | None = None, silent: bool = False):
        from utils.permissions import check_permissions
        error = check_permissions(interaction, name)
        if error:
            raise app_commands.CheckFailure(error)

        await interaction.response.defer(ephemeral=silent)

        path = resolve_script(interaction.guild_id, script_name)
        if path is None:
            await interaction.followup.send(f"Script `{script_name}` not found.", ephemeral=True)
            return

        arg_list = args.split() if args else []
        returncode, stdout, stderr = await run_script(path, arg_list)

        embed = ScriptsCog._build_embed(script_name, returncode, stdout, stderr)
        await interaction.followup.send(embed=embed)

    cmd = app_commands.Command(name=name, description=description, callback=callback)
    return cmd


class ShortcutsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        global_shortcuts, guild_shortcuts = _load_shortcuts()

        existing = {cmd.name for cmd in self.bot.tree.get_commands()}

        # Register global shortcuts
        for name, conf in global_shortcuts.items():
            if name in RESERVED_NAMES or name in existing:
                logger.warning(f"Shortcut '{name}' conflicts with existing command, skipping")
                continue
            cmd = _make_command(name, conf)
            self.bot.tree.add_command(cmd)
            existing.add(name)

        # Register guild-specific shortcuts
        for guild_id, shortcuts in guild_shortcuts.items():
            guild_obj = discord.Object(id=guild_id)
            for name, conf in shortcuts.items():
                if name in RESERVED_NAMES:
                    logger.warning(f"Shortcut '{name}' conflicts with reserved command, skipping")
                    continue
                cmd = _make_command(name, conf)
                self.bot.tree.add_command(cmd, guild=guild_obj)

    async def cog_unload(self):
        global_shortcuts, guild_shortcuts = _load_shortcuts()

        for name in global_shortcuts:
            self.bot.tree.remove_command(name)

        for guild_id, shortcuts in guild_shortcuts.items():
            guild_obj = discord.Object(id=guild_id)
            for name in shortcuts:
                self.bot.tree.remove_command(name, guild=guild_obj)


async def setup(bot: commands.Bot):
    await bot.add_cog(ShortcutsCog(bot))
