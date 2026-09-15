"""Standalone tests for gitops streamed mode (live progress + stall timeout).

Run with:  venv/bin/python tests/local_gitprogress_tests.py
No Discord connection needed.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent.parent))

import utils.gitops as gitops  # noqa: E402
from utils.gitops import GitError, git  # noqa: E402

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": "tester@test.invalid",
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": "tester@test.invalid",
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


def fake_process(script: str):
    """Patch gitops' subprocess launch to run a bash script instead of git."""
    real = asyncio.create_subprocess_exec

    async def launcher(_git, *args, **kwargs):
        return await real("/bin/bash", "-c", script, **kwargs)

    return mock.patch.object(gitops.asyncio, "create_subprocess_exec", launcher)


async def run_streamed(script: str, timeout: int):
    lines = []

    async def on_progress(line: str):
        lines.append(line)

    with fake_process(script):
        rc, out, err = await git("fetch", timeout=timeout, on_progress=on_progress, env=GIT_ENV)
    return rc, out, err, lines


def test_streaming_mechanics():
    print("streaming mechanics:")

    # \r-delimited in-place progress updates reach the callback.
    rc, _, _, lines = asyncio.run(run_streamed(
        r"printf 'step 1\rstep 2\rstep 3\n' >&2", timeout=5
    ))
    check("exit 0", rc == 0)
    check("progress lines seen", lines and lines[-1] == "step 3", str(lines))

    # Steady output slower than nothing but faster than the stall window survives.
    rc, _, _, lines = asyncio.run(run_streamed(
        "for i in 1 2 3 4 5 6; do echo tick $i >&2; sleep 0.3; done", timeout=1
    ))
    check("active transfer outlives stall window", rc == 0 and len(lines) == 6, str(lines))

    # Total silence for the stall window kills the process.
    try:
        asyncio.run(run_streamed("sleep 30", timeout=1))
        check("silent process killed", False, "no GitError raised")
    except GitError as e:
        check("silent process killed", "stalled" in str(e), str(e))

    # stdout is drained (a full stdout pipe must not deadlock stderr streaming).
    rc, out, _, _ = asyncio.run(run_streamed(
        "head -c 200000 /dev/zero | tr '\\0' 'x'; echo done >&2", timeout=5
    ))
    check("large stdout drained", rc == 0 and len(out) == 200000, f"len={len(out)}")

    # A failing streamed command raises GitError with the stderr tail.
    try:
        asyncio.run(run_streamed("echo boom-detail >&2; exit 128", timeout=5))
        check("failure raises GitError", False, "no GitError raised")
    except GitError as e:
        check("failure raises GitError", "boom-detail" in e.stderr, repr(e.stderr))

    # A broken callback must never kill the transfer.
    async def broken(_line):
        raise RuntimeError("callback bug")

    async def run_broken():
        with fake_process("echo hi >&2"):
            return await git("fetch", timeout=5, on_progress=broken, env=GIT_ENV)

    rc, _, _ = asyncio.run(run_broken())
    check("broken callback ignored", rc == 0)


def test_real_git_clone():
    print("real git clone with --progress:")
    tmp = Path(tempfile.mkdtemp(prefix="gandalf-progress-test-"))
    src = tmp / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=src, env=GIT_ENV, check=True)
    for i in range(3):
        (src / f"f{i}.txt").write_text("x" * 5000)
        subprocess.run(["git", "add", "-A"], cwd=src, env=GIT_ENV, check=True)
        subprocess.run(["git", "commit", "-q", "-m", f"c{i}"], cwd=src, env=GIT_ENV, check=True)

    lines = []

    async def on_progress(line: str):
        lines.append(line)

    dest = tmp / "clone"
    # file:// forces the transport path, so git emits real progress output.
    rc, _, _ = asyncio.run(git(
        "clone", "--progress", f"file://{src}", str(dest),
        timeout=30, on_progress=on_progress, env=GIT_ENV,
    ))
    check("clone succeeded", rc == 0 and (dest / ".git").exists())
    check("git progress lines streamed", any("objects" in l.lower() for l in lines), str(lines[:5]))


if __name__ == "__main__":
    test_streaming_mechanics()
    test_real_git_clone()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
