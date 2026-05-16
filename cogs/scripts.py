import discord
from discord import app_commands
from discord.ext import commands

from utils.runner import run_script, resolve_script, list_scripts
from utils.permissions import require_permissions


class ScriptsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="run", description="Run a script")
    @app_commands.describe(
        script="Script name to execute",
        args="Space-separated arguments",
        silent="Send result only to you",
    )
    @require_permissions()
    async def run_cmd(
        self,
        interaction: discord.Interaction,
        script: str,
        args: str | None = None,
        silent: bool = False,
    ):
        await interaction.response.defer(ephemeral=silent)

        path = resolve_script(interaction.guild_id, script)
        if path is None:
            await interaction.followup.send(f"Script `{script}` not found.", ephemeral=True)
            return

        arg_list = args.split() if args else []
        returncode, stdout, stderr = await run_script(path, arg_list)

        embed = self._build_embed(script, returncode, stdout, stderr)
        await interaction.followup.send(embed=embed)

    @run_cmd.autocomplete("script")
    async def run_autocomplete(self, interaction: discord.Interaction, current: str):
        scripts = list_scripts(interaction.guild_id)
        return [
            app_commands.Choice(name=f"{name} ({source})", value=name)
            for name, source in scripts
            if current.lower() in name.lower()
        ][:25]

    @app_commands.command(name="list", description="List available scripts")
    @require_permissions()
    async def list_cmd(self, interaction: discord.Interaction):
        scripts = list_scripts(interaction.guild_id)
        if not scripts:
            await interaction.response.send_message("No scripts available.", ephemeral=True)
            return

        lines = [f"`{name}` ({source})" for name, source in scripts]
        embed = discord.Embed(title="Available Scripts", description="\n".join(lines), color=0x5865F2)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staticmethod
    def _build_embed(script: str, returncode: int, stdout: str, stderr: str) -> discord.Embed:
        timed_out = returncode == -1
        success = returncode == 0

        if timed_out:
            color = 0xFFA500
            title = f"\u23F1 {script} — Timed Out"
        elif success:
            color = 0x2ECC71
            title = f"\u2705 {script} — Success"
        else:
            color = 0xE74C3C
            title = f"\u274C {script} — Failed"

        embed = discord.Embed(title=title, color=color)
        if stdout:
            embed.add_field(name="stdout", value=f"```\n{stdout[:1000]}\n```", inline=False)
        if stderr:
            embed.add_field(name="stderr", value=f"```\n{stderr[:1000]}\n```", inline=False)
        embed.set_footer(text=f"Exit code: {returncode}")
        return embed


async def setup(bot: commands.Bot):
    await bot.add_cog(ScriptsCog(bot))
