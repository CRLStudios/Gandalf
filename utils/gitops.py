import asyncio
import os
import re
from pathlib import Path
from typing import Awaitable, Callable

from config import GIT_TIMEOUT

# git progress lines are delimited by \r (in-place updates) as well as \n.
_LINE_BREAK = re.compile(rb"[\r\n]")

ASKPASS_SCRIPT = Path(__file__).parent / "git-askpass.sh"


class GitError(Exception):
    """A git command failed in a way the merge flow cannot recover from."""

    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def build_git_env(token: str | None = None) -> dict[str, str]:
    """Environment for git subprocesses: never prompt, auth via askpass."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes"),
    }
    if token:
        env["GIT_ASKPASS"] = str(ASKPASS_SCRIPT)
        env["GIT_ASKPASS_PASSWORD"] = token
    return env


async def git(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = GIT_TIMEOUT,
    check: bool = True,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr).

    With check=True, raises GitError on non-zero exit or timeout.

    With on_progress set, stderr is streamed live and the callback receives
    the latest line (pass --progress in args for transfer commands). The
    timeout then becomes a STALL timeout: the command dies only after that
    many seconds with no output at all, so a slow-but-moving transfer is
    never killed mid-download.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env or build_git_env(),
    )
    if on_progress is None:
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise GitError(f"`git {args[0]}` timed out after {timeout}s")

        stdout = (stdout_bytes or b"").decode(errors="replace")
        stderr = (stderr_bytes or b"").decode(errors="replace")
        if check and proc.returncode != 0:
            raise GitError(f"`git {args[0]}` failed (exit {proc.returncode})", stderr=stderr.strip())
        return proc.returncode, stdout, stderr

    # Streamed mode. stdout is drained concurrently so a full pipe can
    # never deadlock the transfer.
    stdout_task = asyncio.create_task(proc.stdout.read())
    stderr_chunks: list[bytes] = []
    pending = b""
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stderr.read(4096), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise GitError(
                    f"`git {args[0]}` stalled — no output for {timeout}s"
                )
            if not chunk:
                break
            stderr_chunks.append(chunk)
            *lines, pending = _LINE_BREAK.split(pending + chunk)
            for raw in reversed(lines):
                text = raw.decode(errors="replace").strip()
                if text:
                    try:
                        await on_progress(text)
                    except Exception:
                        pass  # a broken callback must never kill the transfer
                    break  # only the newest line matters
        stdout_bytes = await stdout_task
        rc = await proc.wait()
    finally:
        stdout_task.cancel()

    stderr = b"".join(stderr_chunks).decode(errors="replace")
    if check and rc != 0:
        # The interesting part of a failed transfer's stderr is the tail;
        # the head is thousands of progress updates.
        raise GitError(f"`git {args[0]}` failed (exit {rc})", stderr=stderr.strip()[-800:])
    return rc, stdout_bytes.decode(errors="replace"), stderr
