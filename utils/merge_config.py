"""Per-guild merge config: one repo and conflict handler, any number of merge sets.

A merge set is a dev branch plus the user branches merged into it, with its
own daily schedule and round-up baseline. Sets are keyed by their dev branch;
`default` names the set that runs when a command doesn't say which one.
"""

import json
from pathlib import Path

from config import MERGECONFIGS_DIR


class MergeConfigError(Exception):
    """A config lookup or change that can't be done; str() is the user-facing reply."""


def _new_set() -> dict:
    return {
        "branches": [],    # user branches merged into this set's dev branch
        "schedule": None,  # {"time": "HH:MM", "channel_id": int} for daily auto-merge
        "roundup": None,   # {"repo": name, "baseline": sha} — advances when a round-up posts
    }


def _new_config() -> dict:
    return {
        "repo": None,          # repo name as registered via /repo add
        "ping_user_id": None,  # who gets pinged on conflict (falls back to admin_user_id)
        "default": None,       # dev branch of the set used when none is named
        "sets": {},            # dev branch -> merge set
    }


def all_guild_ids() -> list[int]:
    """Guild IDs that have a saved merge config."""
    if not MERGECONFIGS_DIR.is_dir():
        return []
    ids = []
    for path in MERGECONFIGS_DIR.glob("*.json"):
        try:
            ids.append(int(path.stem))
        except ValueError:
            continue
    return ids


def _config_path(guild_id: int) -> Path:
    return MERGECONFIGS_DIR / f"{guild_id}.json"


def _normalize(data: dict) -> dict:
    config = _new_config()
    config["repo"] = data.get("repo")
    config["ping_user_id"] = data.get("ping_user_id")
    if isinstance(data.get("sets"), dict):
        for dev, merge_set in data["sets"].items():
            config["sets"][dev] = {**_new_set(), **merge_set}
        config["default"] = data.get("default")
    elif data.get("dev_branch"):
        # Pre-sets file: its single dev branch + branch list become the default set.
        dev = data["dev_branch"]
        config["sets"][dev] = {
            "branches": [b for b in data.get("branches", []) if b != dev],
            "schedule": data.get("schedule"),
            "roundup": data.get("roundup"),
        }
        config["default"] = dev
    if config["default"] not in config["sets"]:
        config["default"] = next(iter(config["sets"]), None)
    return config


def load_merge_config(guild_id: int) -> dict:
    path = _config_path(guild_id)
    if not path.is_file():
        return _new_config()
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        # A corrupt file must not brick /merge and /mergeconfig for the guild.
        return _new_config()
    if not isinstance(data, dict):
        return _new_config()
    return _normalize(data)


def save_merge_config(guild_id: int, config: dict):
    MERGECONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    path = _config_path(guild_id)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    tmp.replace(path)


# ------------------------------------------------------------------ merge sets


def resolve_set(config: dict, requested: str | None = None) -> str:
    """Dev branch of the set a command should act on: the named one, else the default."""
    if not config["sets"]:
        raise MergeConfigError("No merge set yet — create one with `/mergeconfig create`.")
    if requested is None:
        return config["default"]
    if requested not in config["sets"]:
        raise MergeConfigError(
            f"No merge set with dev branch `{requested}` — see `/mergeconfig show`."
        )
    return requested


def branch_owner(config: dict, branch: str) -> str | None:
    """Dev branch of the set that uses `branch`, as its dev branch or a user branch."""
    for dev, merge_set in config["sets"].items():
        if branch == dev or branch in merge_set["branches"]:
            return dev
    return None


def _require_set(config: dict, dev: str) -> dict:
    if dev not in config["sets"]:
        raise MergeConfigError(f"No merge set with dev branch `{dev}` — see `/mergeconfig show`.")
    return config["sets"][dev]


def _require_unused_dev(config: dict, dev: str):
    if dev in config["sets"]:
        raise MergeConfigError(f"A merge set for `{dev}` already exists.")
    owner = branch_owner(config, dev)
    if owner:
        raise MergeConfigError(
            f"`{dev}` is a user branch in the `{owner}` set — a dev branch can't also be a user branch."
        )


def create_set(config: dict, dev: str):
    """Add an empty set for `dev`; the first set created becomes the default."""
    _require_unused_dev(config, dev)
    config["sets"][dev] = _new_set()
    if config["default"] is None:
        config["default"] = dev


def rename_set(config: dict, dev: str, new_dev: str):
    """Point a set at a different dev branch, keeping its branches, schedule and baseline."""
    _require_set(config, dev)
    if new_dev == dev:
        raise MergeConfigError(f"The set's dev branch is already `{dev}`.")
    _require_unused_dev(config, new_dev)
    config["sets"] = {new_dev if k == dev else k: v for k, v in config["sets"].items()}
    if config["default"] == dev:
        config["default"] = new_dev


def delete_set(config: dict, dev: str) -> dict:
    """Remove a set and return it. The default set can only go last."""
    _require_set(config, dev)
    if config["default"] == dev and len(config["sets"]) > 1:
        raise MergeConfigError(
            f"`{dev}` is the default set — make another set the default first with `/mergeconfig default`."
        )
    removed = config["sets"].pop(dev)
    if not config["sets"]:
        config["default"] = None
    return removed


def set_default(config: dict, dev: str):
    _require_set(config, dev)
    config["default"] = dev


def add_branch(config: dict, dev: str, branch: str):
    """Add a user branch to a set. A branch belongs to one set only: synced
    back from two dev branches, it would carry each one's history into the other."""
    merge_set = _require_set(config, dev)
    if branch in config["sets"]:
        raise MergeConfigError(
            f"`{branch}` is the dev branch of a merge set — it can't also be a user branch."
        )
    owner = branch_owner(config, branch)
    if owner == dev:
        raise MergeConfigError(f"`{branch}` is already in the `{dev}` merge list.")
    if owner:
        raise MergeConfigError(
            f"`{branch}` is already a user branch in the `{owner}` set — a branch can belong to one set only."
        )
    merge_set["branches"].append(branch)


def remove_branch(config: dict, dev: str, branch: str):
    merge_set = _require_set(config, dev)
    if branch not in merge_set["branches"]:
        raise MergeConfigError(f"`{branch}` is not in the `{dev}` merge list.")
    merge_set["branches"].remove(branch)
