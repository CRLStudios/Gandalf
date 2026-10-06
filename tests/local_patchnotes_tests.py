"""Standalone tests for utils.patchnotes against scratch git repos.

Each test builds an "origin" repo and a clone of it standing in for the
bot's workspace, so remote branches and mirrored tags behave as they do
in production.

Run with:  venv/bin/python tests/local_patchnotes_tests.py
No Discord connection needed.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.patchnotes import (  # noqa: E402
    PatchNotesError,
    fetch_refs,
    normalize_version,
    parse_version,
    resolve_range,
    version_tags,
)
from utils.roundup import collect_roundup  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Alice",
    "GIT_AUTHOR_EMAIL": "alice@test.invalid",
    "GIT_COMMITTER_NAME": "Alice",
    "GIT_COMMITTER_EMAIL": "alice@test.invalid",
    "GIT_TERMINAL_PROMPT": "0",
}

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, env=GIT_ENV,
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def commit(repo: Path, message: str, tag: str | None = None, filename: str = "file.txt") -> str:
    (repo / filename).write_text(message + "\n" + os.urandom(4).hex())
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-m", message)
    if tag:
        run_git(repo, "tag", tag)
    return run_git(repo, "rev-parse", "HEAD")


def make_origin(tmp: str) -> Path:
    origin = Path(tmp) / "origin"
    origin.mkdir()
    run_git(origin, "init", "-q", "-b", "main")
    return origin


def make_workspace(tmp: str) -> Path:
    run_git(Path(tmp), "clone", "-q", "origin", "workspace")
    return Path(tmp) / "workspace"


def notes_for(workspace: Path, branch: str, version: str | None = None):
    """(range, groups) the way the cog produces them, after a fresh fetch."""
    async def go():
        await fetch_refs(workspace, GIT_ENV)
        notes = await resolve_range(workspace, GIT_ENV, branch, version)
        return notes, await collect_roundup(workspace, GIT_ENV, notes.base, notes.tip)
    return asyncio.run(go())


def error_for(workspace: Path, branch: str, version: str | None = None) -> str | None:
    try:
        notes_for(workspace, branch, version)
    except PatchNotesError as e:
        return str(e)
    return None


# ------------------------------------------------------------------ versions

def test_versions():
    print("version parsing:")
    check("strict tag parses", parse_version("v1.0.3") == (1, 0, 3))
    check("multi-digit parts", parse_version("v12.34.567") == (12, 34, 567))
    check("suffix rejected", parse_version("v1.0.3-rc1") is None)
    check("two parts rejected", parse_version("v1.0") is None)
    check("no v rejected", parse_version("1.0.3") is None)
    check("unrelated tag rejected", parse_version("build-42") is None)
    check("normalize keeps a proper tag", normalize_version("v1.0.3") == "v1.0.3")
    check("normalize adds the v", normalize_version("1.0.3") == "v1.0.3")
    check("normalize lower-cases the v and trims", normalize_version(" V1.0.3 ") == "v1.0.3")
    check("normalize rejects a suffix", normalize_version("v1.0.3-rc1") is None)
    check("normalize rejects words", normalize_version("latest") is None)
    check("normalize rejects empty", normalize_version("") is None)


# ------------------------------------------------------------ linear history

def test_linear():
    print("linear release history:")
    with tempfile.TemporaryDirectory() as tmp:
        origin = make_origin(tmp)
        commit(origin, "[Dev] in v1.0.0", tag="v1.0.0")
        commit(origin, "[Dev] in v1.0.1")
        run_git(origin, "tag", "-a", "-m", "annotated release", "v1.0.1")
        commit(origin, "[Dev] in v1.0.2 first")
        run_git(origin, "tag", "v1.0.2-rc1")
        run_git(origin, "tag", "build-7")
        commit(origin, "[Fix] in v1.0.2 second", tag="v1.0.2")
        head = commit(origin, "[Art] in v1.0.3\nuntagged line follows the tag", tag="v1.0.3")
        workspace = make_workspace(tmp)

        # 1. Head carries a version tag -> that version, since the one before.
        notes, groups = notes_for(workspace, "main")
        check("1. tagged head: version is the head's tag", notes.version == "v1.0.3", str(notes))
        check("1. tagged head: previous is v1.0.2", notes.previous == "v1.0.2", str(notes))
        check("1. tagged head: tip is the head commit", notes.tip == head, str(notes))
        check(
            "1. tagged head: only the newer commit, incl. the tagged one",
            groups == {"art": ["in v1.0.3", "untagged line follows the tag"]}, str(groups),
        )

        # 3. Explicit version -> previous..that tag.
        notes, groups = notes_for(workspace, "main", "v1.0.2")
        check("3. explicit version: previous is v1.0.1", notes.previous == "v1.0.1", str(notes))
        check(
            "3. explicit version: both commits of the release, nothing else",
            groups == {"dev": ["in v1.0.2 first"], "fix": ["in v1.0.2 second"]}, str(groups),
        )

        # 5. Non-strict tags are never a version or a previous version.
        tags = asyncio.run(version_tags(workspace, GIT_ENV, "refs/remotes/origin/main"))
        check(
            "5. only strict tags listed, highest first",
            tags == ["v1.0.3", "v1.0.2", "v1.0.1", "v1.0.0"], str(tags),
        )

        # 10. Annotated tags behave like lightweight ones, as target and as previous.
        notes, groups = notes_for(workspace, "main", "v1.0.1")
        check("10. annotated target: previous is v1.0.0", notes.previous == "v1.0.0", str(notes))
        check("10. annotated target: its own commit only", groups == {"dev": ["in v1.0.1"]}, str(groups))

        # 4. First version -> the whole history up to it.
        notes, groups = notes_for(workspace, "main", "v1.0.0")
        check("4. first version: no previous, no base", notes.previous is None and notes.base is None, str(notes))
        check("4. first version: everything up to the tag", groups == {"dev": ["in v1.0.0"]}, str(groups))

        # 2. Untagged head -> unreleased changes since the latest version.
        head = commit(origin, "[Ui] not released yet")
        notes, groups = notes_for(workspace, "main")
        check("2. untagged head: no version", notes.version is None, str(notes))
        check("2. untagged head: since v1.0.3", notes.previous == "v1.0.3", str(notes))
        check("2. untagged head: tip is the new head", notes.tip == head, str(notes))
        check("2. untagged head: only unreleased work", groups == {"ui": ["not released yet"]}, str(groups))

        # 6. Versions order numerically, not alphabetically.
        commit(origin, "[Dev] in v1.0.9", tag="v1.0.9")
        commit(origin, "[Dev] in v1.0.10", tag="v1.0.10")
        notes, groups = notes_for(workspace, "main")
        check("6. v1.0.10 is the head version", notes.version == "v1.0.10", str(notes))
        check("6. v1.0.10 follows v1.0.9", notes.previous == "v1.0.9", str(notes))
        check("6. v1.0.10 covers one commit", groups == {"dev": ["in v1.0.10"]}, str(groups))
        notes, groups = notes_for(workspace, "main", "v1.0.9")
        check("6. v1.0.9 follows v1.0.3", notes.previous == "v1.0.3", str(notes))
        check(
            "6. v1.0.9 picks up the formerly unreleased work",
            groups == {"ui": ["not released yet"], "dev": ["in v1.0.9"]}, str(groups),
        )

        # Two versions on one commit: neither is the other's previous.
        commit(origin, "[Dev] double-tagged", tag="v1.0.11")
        run_git(origin, "tag", "v1.0.12")
        notes, groups = notes_for(workspace, "main")
        check("same commit: head version is the higher tag", notes.version == "v1.0.12", str(notes))
        check("same commit: previous skips the sibling tag", notes.previous == "v1.0.10", str(notes))
        check("same commit: range is not empty", groups == {"dev": ["double-tagged"]}, str(groups))
        notes, _ = notes_for(workspace, "main", normalize_version("1.0.11"))
        check(
            "same commit: lower sibling, asked for without the v",
            (notes.version, notes.previous) == ("v1.0.11", "v1.0.10"), str(notes),
        )


# -------------------------------------------------------- branching history

def test_branches():
    print("hotfixes and unrelated branches:")
    with tempfile.TemporaryDirectory() as tmp:
        origin = make_origin(tmp)
        commit(origin, "[Dev] in v1.0.3", tag="v1.0.3")
        run_git(origin, "branch", "hotfix")
        run_git(origin, "branch", "other")
        commit(origin, "[Dev] feature in v1.1.0", tag="v1.1.0")

        # The hotfix is tagged after v1.1.0 shipped, then merged forward.
        run_git(origin, "checkout", "-q", "hotfix")
        commit(origin, "[Fix] hotfix in v1.0.4", tag="v1.0.4", filename="hotfix.txt")
        run_git(origin, "checkout", "-q", "main")
        run_git(origin, "merge", "-q", "--no-ff", "-m", "[Dev] merge commit — must not appear", "hotfix")
        commit(origin, "[Dev] more in v1.1.1", tag="v1.1.1")

        # A line that never reaches main, with a higher version on it.
        run_git(origin, "checkout", "-q", "other")
        commit(origin, "[Dev] other line", tag="v9.9.9", filename="other.txt")
        run_git(origin, "checkout", "-q", "main")
        workspace = make_workspace(tmp)

        # 7. Out-of-order hotfix: the nearer v1.0.4 is not the baseline.
        notes, groups = notes_for(workspace, "main")
        check("7. v1.1.1 compares against v1.1.0", (notes.version, notes.previous) == ("v1.1.1", "v1.1.0"), str(notes))
        check(
            "7. the merged hotfix is new to v1.1.1; merge commit skipped",
            groups == {"fix": ["hotfix in v1.0.4"], "dev": ["more in v1.1.1"]}, str(groups),
        )
        notes, groups = notes_for(workspace, "main", "v1.0.4")
        check("7. the hotfix itself compares against v1.0.3", notes.previous == "v1.0.3", str(notes))
        check("7. the hotfix covers its own commit", groups == {"fix": ["hotfix in v1.0.4"]}, str(groups))
        notes, groups = notes_for(workspace, "main", "v1.1.0")
        check("7. v1.1.0 compares against v1.0.3, not the later v1.0.4", notes.previous == "v1.0.3", str(notes))
        check("7. v1.1.0 does not contain the hotfix", groups == {"dev": ["feature in v1.1.0"]}, str(groups))

        # 8. A tag outside the branch's history is never used, and can't be asked for.
        commit(origin, "[Dev] unreleased on main")
        notes, groups = notes_for(workspace, "main")
        check("8. unrelated v9.9.9 is not main's latest version", notes.previous == "v1.1.1", str(notes))
        check("8. main's unreleased work only", groups == {"dev": ["unreleased on main"]}, str(groups))
        err = error_for(workspace, "main", "v9.9.9")
        check("8. version not on the branch is refused", err is not None and "v9.9.9" in err and "main" in err, str(err))
        notes, groups = notes_for(workspace, "other", "v9.9.9")
        check("8. same version on its own branch works", notes.previous == "v1.0.3", str(notes))
        check("8. ...and covers that branch's commit", groups == {"dev": ["other line"]}, str(groups))
        notes, _ = notes_for(workspace, "other")
        check("8. branch option picks that branch's head", notes.version == "v9.9.9", str(notes))


# -------------------------------------------------------------------- errors

def test_errors():
    print("errors:")
    with tempfile.TemporaryDirectory() as tmp:
        origin = make_origin(tmp)
        commit(origin, "[Dev] never released", tag="build-1")
        run_git(origin, "tag", "v1.0.0-beta")
        workspace = make_workspace(tmp)

        err = error_for(workspace, "main")
        check("9. no version tags on the branch", err is not None and "No version tags" in err, str(err))
        err = error_for(workspace, "nope")
        check("9. branch missing from the remote", err is not None and "nope" in err, str(err))
        err = error_for(workspace, "main", "v7.7.7")
        check("9. unknown version tag", err is not None and "v7.7.7" in err, str(err))


# ------------------------------------------------------------- tag mirroring

def test_mirror():
    print("fetch_refs mirrors the remote's tags:")
    with tempfile.TemporaryDirectory() as tmp:
        origin = make_origin(tmp)
        commit(origin, "[Dev] first", tag="v1.0.0")
        commit(origin, "[Dev] second", tag="v2.0.0")
        workspace = make_workspace(tmp)
        asyncio.run(fetch_refs(workspace, GIT_ENV))

        # A tag made on already-fetched history, a re-pointed tag, a deleted tag.
        third = commit(origin, "[Dev] third")
        run_git(origin, "tag", "v1.5.0", "v1.0.0")
        run_git(origin, "tag", "-f", "v2.0.0")
        run_git(origin, "tag", "-d", "v1.0.0")
        asyncio.run(fetch_refs(workspace, GIT_ENV))

        tags = asyncio.run(version_tags(workspace, GIT_ENV, "refs/remotes/origin/main"))
        check("11. new tag on old history arrives, deleted tag goes", tags == ["v2.0.0", "v1.5.0"], str(tags))
        check(
            "11. re-pointed tag follows the remote",
            run_git(workspace, "rev-parse", "refs/tags/v2.0.0") == third,
        )
        notes, groups = notes_for(workspace, "main")
        check("11. notes use the mirrored tags", (notes.version, notes.previous) == ("v2.0.0", "v1.5.0"), str(notes))
        check("11. ...and cover what is now in the release", groups == {"dev": ["second", "third"]}, str(groups))


if __name__ == "__main__":
    test_versions()
    test_linear()
    test_branches()
    test_errors()
    test_mirror()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
