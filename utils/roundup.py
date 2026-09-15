"""Round-up: digest of tagged commit-message lines that landed on dev.

A commit-message line starting with [Tag] files under that tag —
case-insensitive, displayed Title-cased. Untagged lines, merge commits,
and the bot's own commits are skipped. The report covers baseline..tip;
the baseline advances only when a round-up actually posts.
"""

import re
from pathlib import Path

from utils.gitops import git

BOT_EMAIL = "gandalf@bot.invalid"

# One or more [Tag] markers at the start of a line, then the entry text.
_LEADING_TAGS = re.compile(r"^\s*((?:\[[^\[\]\n]+\]\s*)+)(.*)$")
_FIRST_TAG = re.compile(r"\[([^\[\]\n]+)\]")


def parse_message(message: str) -> list[tuple[str, str]]:
    """(tag, text) per tagged line of one commit message.

    The tag is as typed (caller lower-cases to group); with several
    leading tags the first wins and the rest are dropped from the text.
    """
    entries = []
    for line in message.splitlines():
        match = _LEADING_TAGS.match(line)
        if not match:
            continue
        text = match.group(2).strip()
        tag = _FIRST_TAG.search(match.group(1)).group(1).strip()
        if not text or not tag:
            continue
        entries.append((tag, text))
    return entries


async def collect_roundup(
    workspace: Path, env: dict[str, str], base: str, tip: str
) -> dict[str, list[str]]:
    """Tagged lines from base..tip, grouped by lower-cased tag, oldest first.

    Merge commits and commits authored by the bot are skipped.
    Raises GitError if base is not in the workspace's history (e.g. a
    force-pushed remote) — callers treat that as "no baseline yet".
    """
    _, out, _ = await git(
        "log", "--reverse", "--no-merges", "--format=%ae%x1f%B%x1e",
        f"{base}..{tip}", cwd=workspace, env=env,
    )
    groups: dict[str, list[str]] = {}
    for record in out.split("\x1e"):
        email, sep, body = record.partition("\x1f")
        if not sep or email.strip() == BOT_EMAIL:
            continue
        for tag, text in parse_message(body):
            groups.setdefault(tag.lower(), []).append(text)
    return groups


def format_roundup(groups: dict[str, list[str]], limit: int = 4096) -> str | None:
    """Embed description for the round-up, or None if nothing is tagged.

    Sections are alphabetical; over-long reports drop trailing entries
    and end with an "+N more" note instead of a mid-line cut.
    """
    if not groups:
        return None
    lines: list[str] = []
    for key in sorted(groups):
        if lines:
            lines.append("")
        lines.append(f"**{key.title()}**")
        lines.extend(f"• {text}" for text in groups[key])
    full = "\n".join(lines)
    if len(full) <= limit:
        return full

    reserve = 30  # room for the "+N more" suffix
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > limit - reserve:
            break
        kept.append(line)
        used += len(line) + 1
    # A section header with all its entries dropped is an orphan.
    while kept and kept[-1].startswith("**"):
        kept.pop()
    dropped = sum(1 for line in lines[len(kept):] if line.startswith("• "))
    return "\n".join(kept) + f"\n_… +{dropped} more_"
