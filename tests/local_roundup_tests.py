"""Standalone tests for utils.roundup against a scratch git repo.

Run with:  venv/bin/python tests/local_roundup_tests.py
No Discord connection needed.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.roundup import BOT_EMAIL, collect_roundup, format_roundup_pages, parse_message  # noqa: E402

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


def run_git(repo: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env or GIT_ENV,
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def commit(repo: Path, message: str, env: dict | None = None, filename: str = "file.txt") -> str:
    (repo / filename).write_text(message + "\n" + os.urandom(4).hex())
    run_git(repo, "add", "-A", env=env)
    run_git(repo, "commit", "-m", message, env=env)
    return run_git(repo, "rev-parse", "HEAD", env=env)


# ------------------------------------------------------------- parse_message

def test_parse():
    print("parse_message:")
    check("simple tag", parse_message("[Dev] fixed jump") == [("Dev", "fixed jump")])
    check(
        "tag is sticky until the next tag",
        parse_message("[art] tileset\nfollow-up note\n[UI] pause menu")
        == [("art", "tileset"), ("art", "follow-up note"), ("UI", "pause menu")],
    )
    check(
        "bare tag line: following lines are its items",
        parse_message("[Dev]\nfix one\nfix two")
        == [("Dev", "fix one"), ("Dev", "fix two")],
    )
    check(
        "blank lines are skipped, tag stays active",
        parse_message("[Dev] fix one\n\nfix two")
        == [("Dev", "fix one"), ("Dev", "fix two")],
    )
    check(
        "multiple leading tags: first wins, rest stripped",
        parse_message("[DEV][Audio] boom sound") == [("DEV", "boom sound")],
    )
    check("untagged only", parse_message("plain message\nsecond line") == [])
    check("bare tag alone yields nothing", parse_message("[Dev]") == [])
    check(
        "lines before the first tag are omitted",
        parse_message("intro line\n[Dev] the fix") == [("Dev", "the fix")],
    )
    check("tag mid-line does not count", parse_message("fixed [Dev] thing") == [])
    check("leading whitespace ok", parse_message("  [Dev] indented") == [("Dev", "indented")])
    check(
        "leading '* ' bullet stripped on tag line",
        parse_message("[Dev] * bullet item") == [("Dev", "bullet item")],
    )
    check(
        "leading '* ' bullets stripped on section lines",
        parse_message("[Dev]\n* one\n* two") == [("Dev", "one"), ("Dev", "two")],
    )
    check(
        "markdown emphasis is not a bullet",
        parse_message("[Dev] **important** fix") == [("Dev", "**important** fix")],
    )


# ------------------------------------------------------------ format_roundup

def test_format():
    print("format_roundup_pages:")
    check("empty -> []", format_roundup_pages({}) == [])
    pages = format_roundup_pages({"dev": ["a fix"], "art": ["tiles"]})
    check(
        "one page, sections alphabetical, title-cased",
        pages == ["**Art**\n• tiles\n\n**Dev**\n• a fix"],
        repr(pages),
    )

    groups = {"dev": [f"entry number {i}" for i in range(300)], "art": ["tiles"]}
    pages = format_roundup_pages(groups, limit=500)
    check("splits into multiple pages", len(pages) > 1, f"pages={len(pages)}")
    check("every page within limit", all(len(p) <= 500 for p in pages), str([len(p) for p in pages]))
    total_bullets = sum(p.count("• ") for p in pages)
    check("no entry lost or truncated", total_bullets == 301, f"bullets={total_bullets}")
    check(
        "split section continues with (cont.) header",
        any(p.startswith("**Dev** _(cont.)_") for p in pages[1:]),
        repr([p.splitlines()[0] for p in pages]),
    )
    check(
        "no page ends on a bare header",
        all(not p.splitlines()[-1].startswith("**") for p in pages),
        repr([p.splitlines()[-1] for p in pages]),
    )
    check(
        "entries stay in order across pages",
        "\n".join(pages).count("entry number 0\n") >= 1
        and [f"entry number {i}" in "\n".join(pages) for i in (0, 150, 299)] == [True] * 3,
    )

    # A single entry longer than a whole page is hard-truncated, not dropped.
    pages = format_roundup_pages({"dev": ["x" * 900]}, limit=500)
    check("monster entry truncated with ellipsis", len(pages) == 1 and pages[0].endswith("…")
          and len(pages[0]) <= 500, str([len(p) for p in pages]))


# ----------------------------------------------------------- collect_roundup

def test_collect():
    print("collect_roundup:")
    tmp = Path(tempfile.mkdtemp(prefix="gandalf-roundup-test-"))
    repo = tmp / "repo"
    repo.mkdir()
    run_git(repo, "init", "-b", "develop")

    commit(repo, "[Dev] before baseline — must not appear")
    baseline = run_git(repo, "rev-parse", "HEAD")

    commit(repo, "[Dev] fixed jump physics")
    commit(repo, "[art] new tileset\nextra art detail\n[UI] pause menu layout")
    commit(repo, "[DEV][Audio] explosion sound")
    commit(repo, "untagged commit — must not appear")

    bot_env = {
        **GIT_ENV,
        "GIT_AUTHOR_NAME": "Gandalf", "GIT_AUTHOR_EMAIL": BOT_EMAIL,
        "GIT_COMMITTER_NAME": "Gandalf", "GIT_COMMITTER_EMAIL": BOT_EMAIL,
    }
    commit(repo, "[Dev] bot-authored — must not appear", env=bot_env)

    # A merge commit whose message carries a tag — must be skipped.
    run_git(repo, "checkout", "-b", "side", f"{baseline}")
    commit(repo, "[Dev] side branch work", filename="side.txt")
    run_git(repo, "checkout", "develop")
    run_git(repo, "merge", "--no-ff", "-m", "[Dev] merge commit — must not appear", "side")

    tip = run_git(repo, "rev-parse", "HEAD")
    groups = asyncio.run(collect_roundup(repo, GIT_ENV, baseline, tip))

    check("keys are lower-cased and merged", set(groups) == {"dev", "art", "ui"}, str(set(groups)))
    check(
        "dev entries oldest-first, case-insensitive, incl. side branch",
        groups.get("dev") == ["fixed jump physics", "explosion sound", "side branch work"],
        str(groups.get("dev")),
    )
    check(
        "art entries incl. sticky follow-up line",
        groups.get("art") == ["new tileset", "extra art detail"],
        str(groups.get("art")),
    )
    check("ui entry from body line", groups.get("ui") == ["pause menu layout"], str(groups.get("ui")))
    joined = str(groups)
    check("baseline/untagged/bot/merge all absent", "must not appear" not in joined, joined)

    empty = asyncio.run(collect_roundup(repo, GIT_ENV, tip, tip))
    check("baseline == tip -> empty", empty == {})


if __name__ == "__main__":
    test_parse()
    test_format()
    test_collect()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
