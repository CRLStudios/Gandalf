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

from utils.roundup import BOT_EMAIL, collect_roundup, format_roundup, parse_message  # noqa: E402

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
        "multi-line, mixed",
        parse_message("[art] tileset\nuntagged note\n[UI] pause menu")
        == [("art", "tileset"), ("UI", "pause menu")],
    )
    check(
        "multiple leading tags: first wins, rest stripped",
        parse_message("[DEV][Audio] boom sound") == [("DEV", "boom sound")],
    )
    check("untagged only", parse_message("plain message\nsecond line") == [])
    check("bare tag with no text is dropped", parse_message("[Dev]") == [])
    check("tag mid-line does not count", parse_message("fixed [Dev] thing") == [])
    check("leading whitespace ok", parse_message("  [Dev] indented") == [("Dev", "indented")])


# ------------------------------------------------------------ format_roundup

def test_format():
    print("format_roundup:")
    check("empty -> None", format_roundup({}) is None)
    out = format_roundup({"dev": ["a fix"], "art": ["tiles"]})
    check(
        "sections alphabetical, title-cased",
        out == "**Art**\n• tiles\n\n**Dev**\n• a fix",
        repr(out),
    )
    groups = {"dev": [f"entry number {i}" for i in range(300)]}
    out = format_roundup(groups, limit=500)
    check("truncated under limit", len(out) <= 500, f"len={len(out)}")
    check("truncation note present", "more_" in out and "+" in out, repr(out[-40:]))
    shown = out.count("• ")
    claimed = int(out.rsplit("+", 1)[1].split()[0])
    check("shown + dropped = total", shown + claimed == 300, f"{shown}+{claimed}")


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
    commit(repo, "[art] new tileset\nrandom untagged body line\n[UI] pause menu layout")
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
    check("art entry", groups.get("art") == ["new tileset"], str(groups.get("art")))
    check("ui entry from body line", groups.get("ui") == ["pause menu layout"], str(groups.get("ui")))
    joined = str(groups)
    check("baseline/untagged/bot/merge all absent",
          "must not appear" not in joined and "untagged" not in joined, joined)

    empty = asyncio.run(collect_roundup(repo, GIT_ENV, tip, tip))
    check("baseline == tip -> empty", empty == {})


if __name__ == "__main__":
    test_parse()
    test_format()
    test_collect()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
