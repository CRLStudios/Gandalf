import asyncio
from pathlib import Path

from config import SCRIPTS_DIR, SCRIPT_TIMEOUT


async def run_script(path: Path, args: list[str], timeout: int = SCRIPT_TIMEOUT, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run a script via /bin/bash and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash", str(path), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", f"Script timed out after {timeout}s"

    stdout = (stdout_bytes or b"").decode(errors="replace")[:1024]
    stderr = (stderr_bytes or b"").decode(errors="replace")[:1024]
    return proc.returncode, stdout, stderr


def resolve_script(guild_id: int, name: str) -> Path | None:
    """Resolve script by name — guild-specific first, then common. Returns None if not found."""
    safe_name = Path(name).name  # strip directory components
    if not safe_name:
        return None

    guild_path = SCRIPTS_DIR / str(guild_id) / safe_name
    if guild_path.is_file():
        return guild_path

    common_path = SCRIPTS_DIR / "common" / safe_name
    if common_path.is_file():
        return common_path

    return None


def list_scripts(guild_id: int) -> list[tuple[str, str]]:
    """Return list of (script_name, source) for a guild. Server scripts shadow common ones."""
    scripts: dict[str, str] = {}

    common_dir = SCRIPTS_DIR / "common"
    if common_dir.is_dir():
        for f in sorted(common_dir.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                scripts[f.name] = "common"

    guild_dir = SCRIPTS_DIR / str(guild_id)
    if guild_dir.is_dir():
        for f in sorted(guild_dir.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                scripts[f.name] = "server"

    return [(name, source) for name, source in sorted(scripts.items())]
