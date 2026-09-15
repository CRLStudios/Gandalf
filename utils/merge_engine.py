"""Atomic team-sync merge flow.

Merges every configured user branch into the development branch locally.
Nothing is pushed unless ALL branches merge cleanly; a single conflict
aborts the whole run and leaves both the remote and the workspace untouched.
After a clean run, dev is pushed and every user branch is fast-forwarded to
match dev, so the whole team ends up on identical history.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from utils.gitops import GitError, build_git_env, git

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]

MAX_CONFLICT_FILES = 20


@dataclass
class BranchResult:
    branch: str
    status: str            # merged | up_to_date | missing | conflict
    commits: int = 0       # commits this branch brought into dev
    sync: str = ""         # synced | in_sync | sync_skipped | "" (run aborted)
    conflict_files: list[str] = field(default_factory=list)


@dataclass
class MergeReport:
    ok: bool
    dev_branch: str
    results: list[BranchResult] = field(default_factory=list)
    conflict: BranchResult | None = None
    error: str | None = None
    pushed: bool = False
    dev_new_commits: int = 0
    dev_head: str | None = None  # dev tip after the run (only set on ok runs)

    @property
    def merged(self) -> list[BranchResult]:
        return [r for r in self.results if r.status == "merged"]

    @property
    def missing(self) -> list[BranchResult]:
        return [r for r in self.results if r.status == "missing"]


async def _noop_progress(_msg: str):
    return None


class MergeEngine:
    def __init__(self, repo_dir: Path, url: str, token: str | None = None):
        self.repo_dir = repo_dir
        self.url = url
        self.env = build_git_env(token)

    async def _git(self, *args: str, check: bool = True, timeout: int | None = None, **kwargs):
        kwargs.update(cwd=self.repo_dir, env=self.env, check=check)
        if timeout is not None:
            kwargs["timeout"] = timeout
        return await git(*args, **kwargs)

    async def _transfer(
        self, label: str, progress: ProgressCallback, *args: str, cwd_ready: bool = True, **kwargs
    ):
        """Run a transfer command (--progress in args) with live feedback.

        The status line shows elapsed time plus git's latest progress line;
        a ticker keeps the clock moving through git's silent phases. The
        stall-based timeout comes from gitops streamed mode.
        """
        start = time.monotonic()
        latest = {"line": ""}

        def compose() -> str:
            mins, secs = divmod(int(time.monotonic() - start), 60)
            elapsed = f"{mins}m {secs:02d}s" if mins else f"{secs}s"
            text = f"{label} ({elapsed})"
            if latest["line"]:
                text += f"\n{latest['line']}"
            return text

        async def on_line(line: str):
            latest["line"] = line
            await progress(compose())

        async def ticker():
            while True:
                await asyncio.sleep(5)
                await progress(compose())

        tick = asyncio.create_task(ticker())
        try:
            if cwd_ready:
                return await self._git(*args, on_progress=on_line, **kwargs)
            return await git(*args, env=self.env, on_progress=on_line, **kwargs)
        finally:
            tick.cancel()

    async def _remote_ref(self, branch: str) -> str | None:
        """Commit hash of origin/<branch>, or None if the branch doesn't exist."""
        rc, out, _ = await self._git(
            "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}", check=False
        )
        return out.strip() if rc == 0 else None

    async def _ensure_workspace(self, progress: ProgressCallback):
        if not (self.repo_dir / ".git").exists():
            self.repo_dir.parent.mkdir(parents=True, exist_ok=True)
            await self._transfer(
                "Cloning repository (first run — this can take a while)…", progress,
                "clone", "--progress", self.url, str(self.repo_dir),
                cwd_ready=False,
            )
        else:
            git_dir = self.repo_dir / ".git"
            # A git command killed on timeout (or a host crash) can't remove its
            # lock files, and git never cleans them up by itself. The per-guild
            # guard in MergeCog means no git process of ours runs concurrently,
            # so any lock here is stale — without this, every index-writing
            # command fails with "index.lock: File exists" forever.
            for lock in [git_dir / "index.lock", git_dir / "packed-refs.lock",
                         *git_dir.glob("refs/**/*.lock")]:
                lock.unlink(missing_ok=True)
            if (git_dir / "MERGE_HEAD").exists():
                await self._git("merge", "--abort", check=False)
            # /repo add may have re-pointed this repo at a new URL; the clone
            # keeps the old remote until told otherwise.
            await self._git("remote", "set-url", "origin", self.url)
            await self._transfer(
                "Fetching latest changes…", progress,
                "fetch", "--prune", "--progress", "origin",
            )
        # Identity for merge commits; idempotent and cheap.
        await self._git("config", "user.name", "Gandalf")
        await self._git("config", "user.email", "gandalf@bot.invalid")

    async def run(
        self,
        dev_branch: str,
        branches: list[str],
        progress: ProgressCallback = _noop_progress,
    ) -> MergeReport:
        # A failing progress callback must never abort a merge run — it could
        # otherwise kill the run between the dev push and the sync-back.
        async def shielded(msg: str):
            try:
                await progress(msg)
            except Exception:
                logger.warning("Progress callback failed", exc_info=True)

        report = MergeReport(ok=False, dev_branch=dev_branch)
        try:
            return await self._run(dev_branch, branches, shielded, report)
        except GitError as e:
            detail = f"\n```\n{e.stderr[:800]}\n```" if e.stderr else ""
            report.error = f"{e}{detail}"
            return report
        except Exception:
            logger.exception("Unexpected merge engine failure")
            report.error = "Unexpected internal error — check bot.log."
            return report

    async def _run(
        self,
        dev_branch: str,
        branches: list[str],
        progress: ProgressCallback,
        report: MergeReport,
    ) -> MergeReport:
        await self._ensure_workspace(progress)

        dev_ref = await self._remote_ref(dev_branch)
        if dev_ref is None:
            report.error = f"Development branch `{dev_branch}` does not exist on the remote."
            return report

        # Reset workspace to a pristine copy of origin/dev. Local branches from
        # earlier runs are deleted from a detached HEAD so a renamed dev branch
        # can never hit a directory/file ref conflict (e.g. dev -> dev/main).
        await self._git("reset", "--hard", check=False)
        await self._git("clean", "-fd", check=False)
        await self._git("checkout", "--detach", dev_ref)
        _, heads_out, _ = await self._git("for-each-ref", "--format=%(refname:short)", "refs/heads")
        for stale in heads_out.strip().splitlines():
            if stale:
                await self._git("branch", "-D", stale, check=False)
        await self._git("checkout", "-B", dev_branch, f"refs/remotes/origin/{dev_branch}")

        # --- Phase 1: merge every user branch into local dev (nothing pushed yet) ---
        for branch in branches:
            branch_ref = await self._remote_ref(branch)
            if branch_ref is None:
                report.results.append(BranchResult(branch, "missing"))
                continue

            rc, _, _ = await self._git(
                "merge-base", "--is-ancestor", branch_ref, "HEAD", check=False
            )
            if rc == 0:
                report.results.append(BranchResult(branch, "up_to_date"))
                continue

            _, count_out, _ = await self._git("rev-list", "--count", f"HEAD..{branch_ref}")
            commits = int(count_out.strip() or 0)

            await progress(f"Merging `{branch}` into `{dev_branch}`…")
            rc, _, merge_err = await self._git(
                "merge", "--no-ff", "--no-edit",
                "-m", f"Gandalf: merge {branch} into {dev_branch}",
                branch_ref,
                check=False,
            )
            if rc != 0:
                _, files_out, _ = await self._git(
                    "diff", "--name-only", "--diff-filter=U", check=False
                )
                conflict_files = [f for f in files_out.strip().splitlines() if f]
                await self._git("merge", "--abort", check=False)
                await self._git("reset", "--hard", f"refs/remotes/origin/{dev_branch}", check=False)
                if not conflict_files:
                    raise GitError(f"Merging `{branch}` failed (not a normal conflict)", stderr=merge_err.strip())
                result = BranchResult(branch, "conflict", conflict_files=conflict_files)
                report.results.append(result)
                report.conflict = result
                return report

            report.results.append(BranchResult(branch, "merged", commits=commits))

        # --- Phase 2: all clean — push dev, then fast-forward user branches to dev ---
        _, dev_count_out, _ = await self._git("rev-list", "--count", f"{dev_ref}..HEAD")
        report.dev_new_commits = int(dev_count_out.strip() or 0)

        if report.dev_new_commits > 0:
            # Fully qualified refspec — a tag sharing the dev branch's name
            # would otherwise make the short form ambiguous and fail every run.
            rc, _, push_err = await self._transfer(
                f"Pushing `{dev_branch}`…", progress,
                "push", "--progress", "origin",
                f"refs/heads/{dev_branch}:refs/heads/{dev_branch}", check=False,
            )
            if rc != 0:
                raise GitError(
                    f"Could not push `{dev_branch}` — someone pushed to it during the run. Just run /merge again.",
                    stderr=push_err.strip(),
                )
            report.pushed = True

        await progress("Syncing user branches back up to date…")
        _, head_out, _ = await self._git("rev-parse", "HEAD")
        dev_head = head_out.strip()
        report.dev_head = dev_head
        for result in report.results:
            if result.status not in ("merged", "up_to_date"):
                continue
            branch_ref = await self._remote_ref(result.branch)
            if branch_ref == dev_head:
                result.sync = "in_sync"
                continue
            # The lease pins the remote ref to the state we fetched: if the user
            # pushed new work mid-run OR deleted their branch, the push is
            # rejected and their branch is left alone (it catches up next run).
            rc, _, sync_err = await self._git(
                "push", f"--force-with-lease=refs/heads/{result.branch}:{branch_ref}",
                "origin", f"{dev_head}:refs/heads/{result.branch}", check=False,
            )
            if rc == 0:
                result.sync = "synced"
            elif "rejected" in sync_err or "stale info" in sync_err:
                result.sync = "sync_skipped"
            else:
                result.sync = "sync_failed"

        report.ok = True
        return report
