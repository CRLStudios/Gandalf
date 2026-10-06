"""Patch notes: the round-up digest cut by release instead of by baseline.

A version tag is a git tag of the strict form vX.Y.Z; any other tag is
ignored. The notes for a version cover everything after the previous
version up to and including the tagged commit, where "previous" is the
highest lower version among the tags in that version's own history. An
untagged branch head gets the unreleased changes since its latest
version. Nothing is stored — the tags are the only state.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from config import GIT_TIMEOUT
from utils.gitops import GitError, git

_VERSION_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


class PatchNotesError(Exception):
    """Patch notes that can't be produced; str() is the user-facing reply."""


@dataclass
class PatchRange:
    base: str | None      # ref the notes start after; None = the start of history
    tip: str              # commit the notes end at (included)
    version: str | None   # version tag the notes are for; None = unreleased head
    previous: str | None  # version tag the notes start after; None = first version


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) of a strict vX.Y.Z tag, else None."""
    match = _VERSION_TAG.match(tag)
    return tuple(int(part) for part in match.groups()) if match else None


def normalize_version(text: str) -> str | None:
    """The version tag a user meant — `1.0.3` and `V1.0.3` both give `v1.0.3` — or None."""
    tag = "v" + text.strip().lstrip("vV")
    return tag if parse_version(tag) else None


async def fetch_refs(workspace: Path, env: dict[str, str]):
    """Fetch branches and mirror the remote's tags.

    The explicit tag refspec matters: a plain fetch never moves a tag that
    was re-pointed on the remote, nor drops one that was deleted there.
    """
    await git(
        "fetch", "--prune", "origin",
        "+refs/heads/*:refs/remotes/origin/*", "+refs/tags/*:refs/tags/*",
        cwd=workspace, env=env,
    )


async def _commit(workspace: Path, env: dict[str, str], ref: str) -> str | None:
    rc, out, _ = await git(
        "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
        cwd=workspace, env=env, check=False,
    )
    return out.strip() if rc == 0 else None


def _versions(tag_output: str) -> list[str]:
    tags = [t for t in tag_output.split() if parse_version(t)]
    return sorted(tags, key=parse_version, reverse=True)


async def version_tags(
    workspace: Path, env: dict[str, str], ref: str, timeout: int = GIT_TIMEOUT
) -> list[str]:
    """Version tags in the history of `ref`, highest version first."""
    _, out, _ = await git(
        "tag", "--list", "v*", "--merged", ref, cwd=workspace, env=env, timeout=timeout
    )
    return _versions(out)


async def resolve_range(
    workspace: Path, env: dict[str, str], branch: str, version: str | None = None
) -> PatchRange:
    """The commits patch notes should cover on `branch`.

    With `version`, the notes for that tag — which must be in the branch's
    history. Without, the notes for the version tagged at the branch head,
    or the unreleased changes since the latest version if the head is
    untagged. Expects fetch_refs() to have run.
    """
    head = await _commit(workspace, env, f"refs/remotes/origin/{branch}")
    if head is None:
        raise PatchNotesError(f"Branch `{branch}` was not found on the remote.")

    if version is None:
        reachable = await version_tags(workspace, env, head)
        if not reachable:
            raise PatchNotesError(f"No version tags (like `v1.0.3`) found on `{branch}`.")
        _, out, _ = await git("tag", "--points-at", head, cwd=workspace, env=env)
        at_head = _versions(out)
        if not at_head:
            latest = reachable[0]
            return PatchRange(base=f"refs/tags/{latest}", tip=head, version=None, previous=latest)
        version, target = at_head[0], head
    else:
        target = await _commit(workspace, env, f"refs/tags/{version}")
        if target is None:
            raise PatchNotesError(f"There is no `{version}` tag on the remote.")
        rc, _, stderr = await git(
            "merge-base", "--is-ancestor", target, head, cwd=workspace, env=env, check=False
        )
        if rc == 1:
            raise PatchNotesError(f"`{version}` is not in the history of `{branch}`.")
        if rc != 0:
            raise GitError(f"`git merge-base` failed (exit {rc})", stderr=stderr.strip())

    # Tags sharing the target's commit are skipped: comparing against one
    # would always give an empty range.
    _, out, _ = await git("tag", "--points-at", target, cwd=workspace, env=env)
    same_commit = set(out.split())
    lower = [
        tag for tag in await version_tags(workspace, env, target)
        if tag not in same_commit and parse_version(tag) < parse_version(version)
    ]
    previous = lower[0] if lower else None
    return PatchRange(
        base=f"refs/tags/{previous}" if previous else None,
        tip=target, version=version, previous=previous,
    )
