"""Standalone integration tests for utils.merge_engine against scratch git repos.

Run with:  venv/bin/python tests/local_merge_tests.py
No Discord connection needed.
"""

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.merge_engine import MergeEngine  # noqa: E402

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


def sh(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        args, cwd=cwd, env=GIT_ENV, capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)}\n{result.stderr}")
    return result.stdout.strip()


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


def origin_ref(origin: Path, branch: str) -> str:
    return sh("git", "-C", str(origin), "rev-parse", branch)


def write_and_push(clone: Path, branch: str, filename: str, content: str, msg: str):
    """Commit on top of the branch's current remote tip, like a member who pulled."""
    sh("git", "-C", str(clone), "checkout", branch)
    sh("git", "-C", str(clone), "fetch", "origin")
    sh("git", "-C", str(clone), "reset", "--hard", f"origin/{branch}")
    (clone / filename).write_text(content)
    sh("git", "-C", str(clone), "add", "-A")
    sh("git", "-C", str(clone), "commit", "-m", msg)
    sh("git", "-C", str(clone), "push", "origin", branch)


def setup_repos(base: Path) -> tuple[Path, Path]:
    """Bare origin with develop/alice/bob branches + a 'user' clone."""
    origin = base / "origin.git"
    clone = base / "userclone"
    sh("git", "init", "--bare", "-b", "develop", str(origin))
    sh("git", "clone", str(origin), str(clone))
    sh("git", "-C", str(clone), "checkout", "-b", "develop")
    (clone / "shared.txt").write_text("line1\nline2\nline3\n")
    sh("git", "-C", str(clone), "add", "-A")
    sh("git", "-C", str(clone), "commit", "-m", "initial")
    sh("git", "-C", str(clone), "push", "origin", "develop")
    for branch in ("alice", "bob"):
        sh("git", "-C", str(clone), "checkout", "-b", branch, "develop")
        sh("git", "-C", str(clone), "push", "origin", branch)
    return origin, clone


def run_engine(engine: MergeEngine, branches: list[str], progress=None):
    async def _run():
        if progress:
            return await engine.run("develop", branches, progress)
        return await engine.run("develop", branches)

    return asyncio.run(_run())


def main():
    base = Path(tempfile.mkdtemp(prefix="gandalf-merge-test-"))
    print(f"Scratch dir: {base}")
    origin, clone = setup_repos(base)
    engine = MergeEngine(base / "workspace", str(origin))

    # --- Test 1: clean run merges everyone and syncs back -------------------
    print("\nTest 1: clean run")
    write_and_push(clone, "alice", "alice.txt", "art assets\n", "alice work")
    write_and_push(clone, "bob", "bob.txt", "level design\n", "bob work")
    report = run_engine(engine, ["alice", "bob", "carol"])
    statuses = {r.branch: r.status for r in report.results}
    check("run ok", report.ok, str(report.error))
    check("alice merged", statuses.get("alice") == "merged", str(statuses))
    check("bob merged", statuses.get("bob") == "merged", str(statuses))
    check("carol missing", statuses.get("carol") == "missing", str(statuses))
    check("dev pushed", report.pushed and report.dev_new_commits > 0)
    dev = origin_ref(origin, "develop")
    check("alice synced to dev", origin_ref(origin, "alice") == dev)
    check("bob synced to dev", origin_ref(origin, "bob") == dev)
    tree = sh("git", "-C", str(origin), "ls-tree", "--name-only", "develop")
    check("dev has both files", "alice.txt" in tree and "bob.txt" in tree, tree)

    # --- Test 2: idempotent rerun -------------------------------------------
    print("\nTest 2: idempotent rerun")
    before = origin_ref(origin, "develop")
    report = run_engine(engine, ["alice", "bob"])
    statuses = {r.branch: r.status for r in report.results}
    check("run ok", report.ok, str(report.error))
    check("all up to date", set(statuses.values()) == {"up_to_date"}, str(statuses))
    check("nothing new on dev", report.dev_new_commits == 0)
    check("origin dev unchanged", origin_ref(origin, "develop") == before)

    # --- Test 3: conflict aborts atomically ---------------------------------
    print("\nTest 3: conflict aborts everything")
    sh("git", "-C", str(clone), "fetch", "origin")
    sh("git", "-C", str(clone), "checkout", "alice")
    sh("git", "-C", str(clone), "reset", "--hard", "origin/alice")
    write_and_push(clone, "alice", "shared.txt", "line1\nALICE\nline3\n", "alice edit")
    sh("git", "-C", str(clone), "checkout", "bob")
    sh("git", "-C", str(clone), "reset", "--hard", "origin/bob")
    write_and_push(clone, "bob", "shared.txt", "line1\nBOB\nline3\n", "bob edit")
    dev_before = origin_ref(origin, "develop")
    alice_before = origin_ref(origin, "alice")
    report = run_engine(engine, ["alice", "bob"])
    check("run not ok", not report.ok)
    check("conflict on bob", report.conflict is not None and report.conflict.branch == "bob",
          str(report.conflict))
    check("conflict file listed", report.conflict and report.conflict.conflict_files == ["shared.txt"],
          str(report.conflict and report.conflict.conflict_files))
    check("origin dev untouched", origin_ref(origin, "develop") == dev_before)
    check("origin alice untouched", origin_ref(origin, "alice") == alice_before)
    ws = base / "workspace"
    check("workspace clean", sh("git", "-C", str(ws), "status", "--porcelain") == "")
    check("no MERGE_HEAD left", not (ws / ".git" / "MERGE_HEAD").exists())

    # --- Test 4: manual resolution then clean rerun -------------------------
    # Follows the instructions the bot pings out: replay the branches that
    # merged cleanly (alice), then the conflicting one (bob), resolve, push.
    print("\nTest 4: manual resolution flow")
    sh("git", "-C", str(clone), "checkout", "develop")
    sh("git", "-C", str(clone), "pull", "origin", "develop")
    sh("git", "-C", str(clone), "merge", "origin/alice")
    rc = subprocess.run(
        ["git", "-C", str(clone), "merge", "origin/bob"], env=GIT_ENV, capture_output=True
    )
    check("manual merge conflicts as expected", rc.returncode != 0)
    (clone / "shared.txt").write_text("line1\nALICE+BOB\nline3\n")
    sh("git", "-C", str(clone), "add", "-A")
    sh("git", "-C", str(clone), "commit", "-m", "resolve conflict")
    sh("git", "-C", str(clone), "push", "origin", "develop")
    report = run_engine(engine, ["alice", "bob"])
    statuses = {r.branch: r.status for r in report.results}
    check("run ok after resolution", report.ok, str(report.error))
    check("branches up to date", set(statuses.values()) <= {"up_to_date", "merged"}, str(statuses))
    dev = origin_ref(origin, "develop")
    check("alice synced", origin_ref(origin, "alice") == dev)
    check("bob synced", origin_ref(origin, "bob") == dev)

    # --- Test 5: user pushes mid-run -> sync skipped, work preserved --------
    print("\nTest 5: mid-run race on sync-back")
    write_and_push(clone, "alice", "alice2.txt", "more art\n", "alice more work")

    raced = False

    async def racing_progress(text: str):
        nonlocal raced
        if text.startswith("Syncing") and not raced:
            raced = True
            write_and_push(clone, "bob", "bob2.txt", "racing work\n", "bob mid-run push")

    report = run_engine(engine, ["alice", "bob"], progress=racing_progress)
    results = {r.branch: r for r in report.results}
    check("run ok", report.ok, str(report.error))
    check("race happened", raced)
    check("bob sync skipped", results["bob"].sync == "sync_skipped", str(results["bob"]))
    check("alice synced", results["alice"].sync in ("synced", "in_sync"), str(results["alice"]))
    bob_tree = sh("git", "-C", str(origin), "ls-tree", "--name-only", "bob")
    check("bob's racing work preserved", "bob2.txt" in bob_tree, bob_tree)

    # --- Test 6: recovery from a crashed half-merged workspace --------------
    print("\nTest 6: crash recovery")
    write_and_push(clone, "bob", "bob3.txt", "even more\n", "bob again")
    dev_sha = sh("git", "-C", str(ws), "rev-parse", "HEAD")
    (ws / ".git" / "MERGE_HEAD").write_text(dev_sha + "\n")
    (ws / "junk.txt").write_text("leftover conflict junk\n")
    report = run_engine(engine, ["alice", "bob"])
    check("run ok after crash state", report.ok, str(report.error))
    check("bob's new work merged", any(
        r.branch == "bob" and r.status == "merged" for r in report.results
    ), str([(r.branch, r.status) for r in report.results]))
    check("workspace clean again", sh("git", "-C", str(ws), "status", "--porcelain") == "")

    # --- Test 7: stale index.lock from a killed git process -----------------
    print("\nTest 7: stale index.lock recovery")
    write_and_push(clone, "alice", "alice3.txt", "art pass 3\n", "alice again")
    (ws / ".git" / "index.lock").touch()
    report = run_engine(engine, ["alice", "bob"])
    check("run ok despite stale lock", report.ok, str(report.error))
    check("lock gone", not (ws / ".git" / "index.lock").exists())

    # --- Test 8: tag sharing the dev branch name ----------------------------
    print("\nTest 8: tag named like the dev branch")
    sh("git", "-C", str(ws), "tag", "-f", "develop")
    write_and_push(clone, "bob", "bob4.txt", "levels 4\n", "bob again")
    report = run_engine(engine, ["alice", "bob"])
    check("push works despite tag shadow", report.ok and report.pushed, str(report.error))

    # --- Test 9: user branch deleted on the remote mid-run ------------------
    print("\nTest 9: branch deleted mid-run is not resurrected")
    write_and_push(clone, "alice", "alice4.txt", "art 4\n", "alice 4")

    deleted = False

    async def deleting_progress(text: str):
        nonlocal deleted
        if text.startswith("Syncing") and not deleted:
            deleted = True
            sh("git", "-C", str(origin), "update-ref", "-d", "refs/heads/bob")

    report = run_engine(engine, ["alice", "bob"], progress=deleting_progress)
    results = {r.branch: r for r in report.results}
    check("run ok", report.ok, str(report.error))
    check("deletion raced", deleted)
    check("bob sync skipped", results["bob"].sync == "sync_skipped", str(results["bob"]))
    rc = subprocess.run(
        ["git", "-C", str(origin), "rev-parse", "--verify", "refs/heads/bob"],
        env=GIT_ENV, capture_output=True,
    )
    check("bob stays deleted", rc.returncode != 0)

    # --- Test 10: /repo add re-points the URL -> workspace must follow ------
    print("\nTest 10: remote URL change is picked up")
    origin2 = base / "origin2.git"
    sh("git", "clone", "--bare", str(origin), str(origin2))
    sh("git", "-C", str(clone), "remote", "add", "origin2", str(origin2))
    sh("git", "-C", str(clone), "fetch", "origin2")
    sh("git", "-C", str(clone), "checkout", "-B", "alice", "origin2/alice")
    (clone / "migrated.txt").write_text("post-migration work\n")
    sh("git", "-C", str(clone), "add", "-A")
    sh("git", "-C", str(clone), "commit", "-m", "work on new host")
    sh("git", "-C", str(clone), "push", "origin2", "alice")
    old_dev = origin_ref(origin, "develop")
    engine2 = MergeEngine(ws, str(origin2))
    report = run_engine(engine2, ["alice"])
    check("run ok on new URL", report.ok, str(report.error))
    check("workspace re-pointed", sh("git", "-C", str(ws), "remote", "get-url", "origin") == str(origin2))
    check("new origin advanced", origin_ref(origin2, "develop") == origin_ref(origin2, "alice"))
    check("old origin untouched", origin_ref(origin, "develop") == old_dev)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
