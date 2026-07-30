from pathlib import Path

from config import WORKSPACES_DIR
from utils.guild_env import load_guild_env


def repo_names(guild_id: int) -> list[str]:
    """Repo names registered for a guild via /repo add."""
    envs = load_guild_env(guild_id)
    names = []
    for k in envs:
        if k.startswith("REPO_") and k.endswith("_URL"):
            name = k[5:-4]  # strip REPO_ and _URL
            if name:
                names.append(name)
    return sorted(names)


def repo_credentials(guild_id: int, name: str) -> tuple[str, str | None] | None:
    """(url, token) for a registered repo, or None if not registered."""
    envs = load_guild_env(guild_id)
    url = envs.get(f"REPO_{name.upper()}_URL")
    if not url:
        return None
    return url, envs.get(f"REPO_{name.upper()}_TOKEN")


def repo_workspace(guild_id: int, name: str) -> Path:
    return WORKSPACES_DIR / str(guild_id) / name.lower()
