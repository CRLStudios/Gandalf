import json
from pathlib import Path

from config import MERGECONFIGS_DIR

DEFAULT_CONFIG = {
    "repo": None,          # repo name as registered via /repo add
    "dev_branch": None,    # the shared development branch
    "branches": [],        # user branches merged into dev_branch
    "ping_user_id": None,  # who gets pinged on conflict (falls back to admin_user_id)
}


def _config_path(guild_id: int) -> Path:
    return MERGECONFIGS_DIR / f"{guild_id}.json"


def load_merge_config(guild_id: int) -> dict:
    path = _config_path(guild_id)
    if not path.is_file():
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        # A corrupt file must not brick /merge and /mergeconfig for the guild.
        return dict(DEFAULT_CONFIG)
    return {**DEFAULT_CONFIG, **data}


def save_merge_config(guild_id: int, config: dict):
    MERGECONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    path = _config_path(guild_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    tmp.replace(path)
