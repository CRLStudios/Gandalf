import json
from pathlib import Path

from discord import app_commands, Interaction

PERMISSIONS_PATH = Path(__file__).parent.parent / "permissions.json"


def _load_permissions() -> dict:
    if PERMISSIONS_PATH.is_file():
        with open(PERMISSIONS_PATH) as f:
            return json.load(f)
    return {}


def get_admin_user_id() -> int | None:
    perms = _load_permissions()
    val = perms.get("admin_user_id", "")
    return int(val) if val else None


def get_allowed_guilds() -> list[int]:
    perms = _load_permissions()
    return [int(g) for g in perms.get("allowed_guilds", [])]


def _get_command_perms(command_name: str) -> dict:
    perms = _load_permissions()
    commands = perms.get("commands", {})
    if command_name in commands:
        return commands[command_name]
    return perms.get("default", {"roles": [], "channels": []})


def is_admin(user_id: int) -> bool:
    admin_id = get_admin_user_id()
    return admin_id is not None and user_id == admin_id


def check_permissions(interaction: Interaction, command_name: str) -> str | None:
    """Check permissions for a given interaction and command name.

    Returns None if allowed, or an error message string if denied.
    """
    if is_admin(interaction.user.id):
        return None

    allowed = get_allowed_guilds()
    if allowed and interaction.guild_id not in allowed:
        return "This server is not authorized."

    cmd_perms = _get_command_perms(command_name)

    # Role check — empty roles = admin-only
    allowed_roles = cmd_perms.get("roles", [])
    if not allowed_roles:
        return "Admin only."
    if not any(r.name in allowed_roles for r in interaction.user.roles):
        return f"Requires one of: {', '.join(allowed_roles)}"

    # Channel check — empty = any channel
    allowed_channels = cmd_perms.get("channels", [])
    if allowed_channels and interaction.channel.name not in allowed_channels:
        return "Not allowed in this channel."

    return None


def resolve_command_name(interaction: Interaction) -> str:
    name = interaction.command.name
    if interaction.command.parent:
        name = f"{interaction.command.parent.name} {interaction.command.name}"
    return name


def autocomplete_allowed(interaction: Interaction) -> bool:
    """Autocomplete callbacks bypass command checks — gate them explicitly."""
    return check_permissions(interaction, resolve_command_name(interaction)) is None


def require_permissions(command_name: str | None = None):
    """Decorator. Checks guild allowlist, role, and channel permissions."""
    async def predicate(interaction: Interaction) -> bool:
        name = command_name if command_name is not None else resolve_command_name(interaction)
        error = check_permissions(interaction, name)
        if error:
            raise app_commands.CheckFailure(error)
        return True

    return app_commands.check(predicate)
