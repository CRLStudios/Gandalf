from pathlib import Path

from dotenv import dotenv_values

from config import ENVS_DIR


def _env_path(guild_id: int) -> Path:
    return ENVS_DIR / f"{guild_id}.env"


def load_guild_env(guild_id: int) -> dict[str, str]:
    path = _env_path(guild_id)
    if not path.is_file():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def set_guild_env(guild_id: int, key: str, value: str):
    # The env file is line-oriented — an embedded newline would let one value
    # smuggle in extra variables (e.g. forging REPO_*_URL/_TOKEN entries).
    if "\n" in key or "\r" in key or "\n" in value or "\r" in value:
        raise ValueError("Env keys and values must not contain newlines.")
    ENVS_DIR.mkdir(parents=True, exist_ok=True)
    envs = load_guild_env(guild_id)
    envs[key] = value
    _write_env(_env_path(guild_id), envs)


def remove_guild_env(guild_id: int, key: str) -> bool:
    envs = load_guild_env(guild_id)
    if key not in envs:
        return False
    del envs[key]
    _write_env(_env_path(guild_id), envs)
    return True


def list_guild_env_keys(guild_id: int) -> list[str]:
    return list(load_guild_env(guild_id).keys())


def _write_env(path: Path, envs: dict[str, str]):
    with open(path, "w") as f:
        for k, v in sorted(envs.items()):
            f.write(f"{k}={v}\n")
