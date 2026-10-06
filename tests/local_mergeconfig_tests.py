"""Standalone tests for utils.merge_config — the merge-set model.

Run with:  venv/bin/python tests/local_mergeconfig_tests.py
No Discord connection needed.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import utils.merge_config as mc  # noqa: E402
from utils.merge_config import (  # noqa: E402
    MergeConfigError,
    add_branch,
    all_guild_ids,
    branch_owner,
    create_set,
    delete_set,
    load_merge_config,
    remove_branch,
    rename_set,
    resolve_set,
    save_merge_config,
    set_default,
)

PASS = 0
FAIL = 0

GUILD = 1234


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def refused(fn, *args) -> str | None:
    """The MergeConfigError message fn raises, or None if it went through."""
    try:
        fn(*args)
    except MergeConfigError as e:
        return str(e)
    return None


def write_raw(data):
    mc.MERGECONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    (mc.MERGECONFIGS_DIR / f"{GUILD}.json").write_text(json.dumps(data))


def main():
    base = Path(tempfile.mkdtemp(prefix="gandalf-mergeconfig-test-"))
    print(f"Scratch dir: {base}")
    mc.MERGECONFIGS_DIR = base / "mergeconfigs"

    # --- Test 1: no file yet ------------------------------------------------
    print("\nTest 1: guild with no config")
    config = load_merge_config(GUILD)
    check("no sets", config["sets"] == {} and config["default"] is None, str(config))
    check("no release branch", config["release_branch"] is None, str(config))
    check("resolve refuses, naming the fix",
          "/mergeconfig create" in (refused(resolve_set, config, None) or ""))
    create_set(config, "develop")
    check("fresh configs don't share state", load_merge_config(GUILD)["sets"] == {})

    # --- Test 2: legacy single-set file migrates ----------------------------
    print("\nTest 2: legacy config migrates to one default set")
    schedule = {"time": "03:30", "channel_id": 42}
    roundup = {"repo": "GAME", "baseline": "abc123"}
    write_raw({
        "repo": "GAME", "dev_branch": "develop", "branches": ["alice", "bob"],
        "ping_user_id": 7, "schedule": schedule, "roundup": roundup,
    })
    config = load_merge_config(GUILD)
    check("repo and ping kept", config["repo"] == "GAME" and config["ping_user_id"] == 7, str(config))
    check("one set named after the dev branch", list(config["sets"]) == ["develop"], str(config))
    check("it is the default", config["default"] == "develop")
    check("branches carried", config["sets"]["develop"]["branches"] == ["alice", "bob"])
    check("schedule carried", config["sets"]["develop"]["schedule"] == schedule)
    check("round-up baseline carried", config["sets"]["develop"]["roundup"] == roundup)
    check("legacy keys gone", "dev_branch" not in config and "branches" not in config, str(config))
    check("file without a release branch loads as unset", config["release_branch"] is None, str(config))

    # --- Test 3: save / load round trip in the new shape --------------------
    print("\nTest 3: round trip")
    config["release_branch"] = "main"
    save_merge_config(GUILD, config)
    on_disk = json.loads((mc.MERGECONFIGS_DIR / f"{GUILD}.json").read_text())
    check("saved in the new shape", "sets" in on_disk and "dev_branch" not in on_disk, str(on_disk))
    check("reload is identical", load_merge_config(GUILD) == config)
    check("release branch survives the round trip", load_merge_config(GUILD)["release_branch"] == "main")
    check("guild listed", all_guild_ids() == [GUILD], str(all_guild_ids()))

    # --- Test 4: legacy file that never had a dev branch --------------------
    print("\nTest 4: legacy config without a dev branch")
    write_raw({"repo": "GAME", "dev_branch": None, "branches": ["alice"], "ping_user_id": None})
    config = load_merge_config(GUILD)
    check("no sets, repo kept", config["sets"] == {} and config["repo"] == "GAME", str(config))

    # --- Test 5: corrupt or dangling files don't brick the guild ------------
    print("\nTest 5: damaged files")
    (mc.MERGECONFIGS_DIR / f"{GUILD}.json").write_text("{not json")
    check("corrupt file loads empty", load_merge_config(GUILD)["sets"] == {})
    write_raw(["not", "a", "dict"])
    check("wrong top-level type loads empty", load_merge_config(GUILD)["sets"] == {})
    write_raw({"default": "gone", "sets": {"develop": {"branches": ["alice"]}}})
    config = load_merge_config(GUILD)
    check("dangling default repaired", config["default"] == "develop", str(config))
    check("missing set keys filled",
          config["sets"]["develop"] == {"branches": ["alice"], "schedule": None, "roundup": None},
          str(config))

    # --- Test 6: creating sets and resolving the target ---------------------
    print("\nTest 6: create and resolve")
    config = load_merge_config(GUILD + 1)
    create_set(config, "develop")
    check("first set becomes the default", config["default"] == "develop")
    create_set(config, "patch-development")
    check("second set does not steal the default", config["default"] == "develop")
    check("omitted set resolves to the default", resolve_set(config, None) == "develop")
    check("named set resolves to itself",
          resolve_set(config, "patch-development") == "patch-development")
    check("unknown set refused, naming the fix",
          "/mergeconfig show" in (refused(resolve_set, config, "nope") or ""))
    check("duplicate set refused", refused(create_set, config, "develop") is not None)

    # --- Test 7: a branch belongs to one set only ---------------------------
    print("\nTest 7: branch overlap rule")
    add_branch(config, "develop", "alice")
    add_branch(config, "patch-development", "alice-patch")
    check("branches recorded per set",
          config["sets"]["develop"]["branches"] == ["alice"]
          and config["sets"]["patch-development"]["branches"] == ["alice-patch"], str(config))
    check("owner of a user branch", branch_owner(config, "alice-patch") == "patch-development")
    check("owner of a dev branch", branch_owner(config, "develop") == "develop")
    check("unowned branch", branch_owner(config, "carol") is None)
    check("same branch twice in one set refused", refused(add_branch, config, "develop", "alice") is not None)
    msg = refused(add_branch, config, "patch-development", "alice")
    check("branch from another set refused, naming that set", msg is not None and "`develop`" in msg, str(msg))
    check("another set's dev branch refused as a user branch",
          refused(add_branch, config, "develop", "patch-development") is not None)
    check("a set's own dev branch refused as its user branch",
          refused(add_branch, config, "develop", "develop") is not None)
    check("user branch refused as a new set", refused(create_set, config, "alice") is not None)
    check("nothing changed by refusals",
          config["sets"]["develop"]["branches"] == ["alice"]
          and config["sets"]["patch-development"]["branches"] == ["alice-patch"]
          and list(config["sets"]) == ["develop", "patch-development"], str(config))
    remove_branch(config, "develop", "alice")
    check("removed", config["sets"]["develop"]["branches"] == [])
    check("removing an absent branch refused", refused(remove_branch, config, "develop", "alice") is not None)
    add_branch(config, "develop", "alice")

    # --- Test 8: rename carries everything ----------------------------------
    print("\nTest 8: rename")
    config["sets"]["develop"]["schedule"] = schedule
    config["sets"]["develop"]["roundup"] = roundup
    check("rename onto another set refused", refused(rename_set, config, "develop", "patch-development") is not None)
    check("rename onto a user branch refused", refused(rename_set, config, "develop", "alice-patch") is not None)
    check("rename onto its own user branch refused", refused(rename_set, config, "develop", "alice") is not None)
    check("rename of an unknown set refused", refused(rename_set, config, "nope", "x") is not None)
    rename_set(config, "develop", "development")
    check("order kept", list(config["sets"]) == ["development", "patch-development"], str(list(config["sets"])))
    check("default follows the rename", config["default"] == "development")
    check("branches, schedule and baseline carried",
          config["sets"]["development"] == {"branches": ["alice"], "schedule": schedule, "roundup": roundup},
          str(config["sets"]["development"]))
    rename_set(config, "patch-development", "patch")
    check("renaming a non-default set leaves the default", config["default"] == "development")
    rename_set(config, "patch", "patch-development")

    # --- Test 9: default and delete -----------------------------------------
    print("\nTest 9: default and delete")
    msg = refused(delete_set, config, "development")
    check("deleting the default refused, naming the fix",
          msg is not None and "/mergeconfig default" in msg, str(msg))
    check("unknown default refused", refused(set_default, config, "nope") is not None)
    set_default(config, "patch-development")
    check("default moved", config["default"] == "patch-development")
    removed = delete_set(config, "development")
    check("deleted set returned", removed["branches"] == ["alice"] and removed["schedule"] == schedule,
          str(removed))
    check("only the other set remains", list(config["sets"]) == ["patch-development"])
    check("its branch is free again", branch_owner(config, "alice") is None)
    check("deleting an unknown set refused", refused(delete_set, config, "development") is not None)
    delete_set(config, "patch-development")
    check("last set can be deleted", config["sets"] == {} and config["default"] is None, str(config))
    create_set(config, "release")
    check("next set created becomes the default", config["default"] == "release")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
