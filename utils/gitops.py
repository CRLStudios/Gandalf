import asyncio
import os
from pathlib import Path

from config import GIT_TIMEOUT

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
) -> tuple[int, str, str]:
    """Run a git command. Returns (returncode, stdout, stderr).

    With check=True, raises GitError on non-zero exit or timeout.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env or build_git_env(),
    )
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
