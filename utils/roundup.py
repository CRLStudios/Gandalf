"""Round-up: digest of tagged commit-message lines that landed on dev.

A [Tag] line starts a section: it and every following line file under
that tag until the next [Tag] line — case-insensitive, displayed
Title-cased. Lines before any tag, merge commits, and the bot's own
commits are skipped. The report covers baseline..tip; the baseline
advances only when a round-up actually posts.
"""

import re
from pathlib import Path

from utils.gitops import git

BOT_EMAIL = "gandalf@bot.invalid"

# One or more [Tag] markers at the start of a line, then the entry text.
_LEADING_TAGS = re.compile(r"^\s*((?:\[[^\[\]\n]+\]\s*)+)(.*)$")
_FIRST_TAG = re.compile(r"\[([^\[\]\n]+)\]")
# A leading "* " bullet — the report renders its own bullets. The space is
# required so markdown emphasis like **bold** survives.
_LEADING_BULLET = re.compile(r"^\*\s+")


def parse_message(message: str) -> list[tuple[str, str]]:
    """(tag, text) entries from one commit message.

    A tag is sticky: a [Tag] line starts a section and every following
    non-blank line is an item in it, until the next [Tag] line. Text on
    the tag line itself is the first item. Lines before any tag are
    omitted. The tag is as typed (caller lower-cases to group); with
    several leading tags the first wins and the rest are dropped.
    """
    entries = []
    active: str | None = None
    for line in message.splitlines():
        match = _LEADING_TAGS.match(line)
        if match:
            tag = _FIRST_TAG.search(match.group(1)).group(1).strip()
            if tag:
                active = tag
            text = match.group(2).strip()
        else:
            text = line.strip()
        text = _LEADING_BULLET.sub("", text)
        if active and text:
            entries.append((active, text))
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


def format_roundup_pages(groups: dict[str, list[str]], limit: int = 4096) -> list[str]:
    """Embed descriptions for the round-up, one per page; [] if nothing tagged.

    Sections are alphabetical and every entry is kept: a report over the
    limit continues on the next page, re-opening a split section with a
    "(cont.)" header. No page ever ends on a bare header.
    """
    if not groups:
        return []
    pages: list[str] = []
    current: list[str] = []
    used = 0

    def add(lines: list[str]):
        nonlocal used
        current.extend(lines)
        used += sum(len(line) + 1 for line in lines)

    def flush():
        nonlocal current, used
        if current:
            pages.append("\n".join(current))
        current, used = [], 0

    for key in sorted(groups):
        header = f"**{key.title()}**"
        in_section = False
        for text in groups[key]:
            bullet = f"• {text}"
            if in_section:
                block = [bullet]
            else:
                block = ([""] if current else []) + [header, bullet]
            needed = sum(len(line) + 1 for line in block)
            if current and used + needed > limit:
                flush()
                block = [f"{header} _(cont.)_" if in_section else header, bullet]
                needed = sum(len(line) + 1 for line in block)
            if used + needed > limit:
                # A single entry longer than a whole page: hard-truncate it.
                room = limit - used - (needed - len(bullet)) - 1
                block[-1] = bullet[: max(room, 0)] + "…"
            add(block)
            in_section = True
    flush()
    return pages
