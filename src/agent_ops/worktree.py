from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from agent_ops.utils import SLOW_GIT_TIMEOUT_S, CommandError, run


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str


@dataclass(frozen=True)
class BranchStatus:
    """Where `branch` lives relative to `origin/<branch>`, after a fetch.

    `ahead`/`behind` are only meaningful when both `local` and `remote` are
    true; they stay 0 otherwise so callers don't have to guard on that too.
    """

    local: bool
    remote: bool
    ahead: int = 0
    behind: int = 0

    @property
    def diverged(self) -> bool:
        return self.ahead > 0 and self.behind > 0


@dataclass(frozen=True)
class BaseDrift:
    """How far a worktree's HEAD is behind `origin/<base>`, as of the last check.

    `checked=False` means "no answer" (no remote base ref to compare
    against — a test/scratch repo, or a worktree git can't even inspect),
    never "not behind": callers must treat it as unknown, not current.
    """

    checked: bool
    fetched: bool
    behind: int
    base_ref: str
    tip: str


def create(
    project_root: Path,
    worktree_dir: str,
    task_id: str,
    branch: str,
    base: str,
    *,
    reuse: bool = False,
) -> Path:
    """Create an isolated worktree for one task, on a fresh branch cut from base.

    With `reuse`, an existing worktree is returned as-is when it is still the
    pristine one an earlier stage pre-created (e.g. `agent dispatch`): checked
    out on `branch` with a clean status. Anything else fails loudly so leftovers
    from a previous run are never silently reused.
    """
    path = project_root / worktree_dir / task_id
    if path.exists():
        if reuse and _pristine_checkout(path, branch):
            return path
        raise FileExistsError(
            f"Worktree {path} already exists. Remove it with `agent worktree remove {task_id}`."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Branch from origin/<base> when it exists: always-fresh base, and it
    # sidesteps a git DWIM trap — with a remote-only base, `worktree add -b`
    # silently ignores -b and checks out a new local <base> branch instead.
    base_ref = base
    fetch = run(
        ["git", "fetch", "origin", base],
        cwd=project_root,
        check=False,
        timeout=SLOW_GIT_TIMEOUT_S,
    )
    if fetch.returncode == 0:
        base_ref = f"origin/{base}"

    # Parallel dispatches race git's .git/config lock ("could not lock config
    # file") while worktree add writes branch tracking — retry with backoff,
    # cleaning up the half-created branch/dir between attempts.
    last_err = ""
    for attempt in range(3):
        proc = run(
            ["git", "worktree", "add", "-b", branch, str(path), base_ref],
            cwd=project_root,
            check=False,
            timeout=SLOW_GIT_TIMEOUT_S,
        )
        if proc.returncode == 0:
            return path
        last_err = proc.stderr.strip() or proc.stdout.strip()
        run(["git", "worktree", "remove", "--force", str(path)], cwd=project_root, check=False)
        if (
            run(["git", "rev-parse", "--verify", branch], cwd=project_root, check=False).returncode
            == 0
        ):
            run(["git", "branch", "-D", branch], cwd=project_root, check=False)
        if "could not lock" not in last_err and attempt == 0:
            break  # non-contention error — retrying won't help
        time.sleep(1.5 * (attempt + 1))
    raise CommandError(f"git worktree add failed for {branch!r}:\n{last_err}")


def _pristine_checkout(path: Path, branch: str) -> bool:
    """True if `path` is checked out on `branch` with nothing modified or untracked."""
    head = run(["git", "branch", "--show-current"], cwd=path, check=False)
    if head.returncode != 0 or head.stdout.strip() != branch:
        return False
    status = run(["git", "status", "--porcelain"], cwd=path, check=False)
    return status.returncode == 0 and not status.stdout.strip()


def remote_branch_status(project_root: Path, branch: str) -> BranchStatus:
    """Local/remote existence and ahead/behind counts for `branch`.

    Fetches `branch` from origin first so the remote picture is current. A
    failed fetch — no remote configured, offline — degrades to whatever is
    known locally rather than raising: recovering a local-only branch must
    still work with no network at all.
    """
    run(
        ["git", "fetch", "origin", branch],
        cwd=project_root,
        check=False,
        timeout=SLOW_GIT_TIMEOUT_S,
    )
    local = (
        run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
            cwd=project_root,
            check=False,
        ).returncode
        == 0
    )
    remote = (
        run(
            ["git", "rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
            cwd=project_root,
            check=False,
        ).returncode
        == 0
    )
    ahead = behind = 0
    if local and remote:
        counts = run(
            ["git", "rev-list", "--left-right", "--count", f"{branch}...origin/{branch}"],
            cwd=project_root,
            check=False,
        )
        parts = counts.stdout.split()
        if counts.returncode == 0 and len(parts) == 2:
            ahead, behind = int(parts[0]), int(parts[1])
    return BranchStatus(local=local, remote=remote, ahead=ahead, behind=behind)


def prune(project_root: Path) -> None:
    """Drop worktree registrations git kept for directories removed by hand.

    Without this, `create_tracking` can find a branch git still considers
    "checked out" at a path that no longer exists on disk and hand back that
    dead path instead of recreating the worktree.
    """
    run(["git", "worktree", "prune"], cwd=project_root, check=False, timeout=SLOW_GIT_TIMEOUT_S)


def base_drift(wt_path: Path, base: str) -> BaseDrift:
    """Fetch `origin/<base>` and report how far `HEAD` in `wt_path` trails it.

    Read-only: never rebases, resets, or otherwise touches the tree — the
    caller decides what to do with the answer. Degrades to `checked=False`
    rather than raising whenever the question can't be answered at all (no
    `origin/<base>` ref, or the worktree itself can't be inspected); a failed
    fetch — whether it exits fast (network down, bad remote) or hangs until
    `SLOW_GIT_TIMEOUT_S` — still falls back to the last-known local
    `origin/<base>`, which is a real if possibly-stale answer, not silence.
    That fallback works because the fetch runs with `check=False`: `run()`
    reports a timeout as a synthetic non-zero `CompletedProcess` rather than
    raising, so both failure shapes are just another non-zero `returncode`
    here, not an exception to catch.
    """
    base_ref = f"origin/{base}"
    fetch = run(
        ["git", "fetch", "origin", base],
        cwd=wt_path,
        check=False,
        timeout=SLOW_GIT_TIMEOUT_S,
    )
    fetched = fetch.returncode == 0
    verify = run(["git", "rev-parse", "--verify", base_ref], cwd=wt_path, check=False)
    if verify.returncode != 0:
        return BaseDrift(False, fetched, 0, base_ref, "")
    counted = run(["git", "rev-list", "--count", f"HEAD..{base_ref}"], cwd=wt_path, check=False)
    if counted.returncode != 0:
        return BaseDrift(False, fetched, 0, base_ref, "")
    tipped = run(["git", "log", "-1", "--format=%h %s", base_ref], cwd=wt_path, check=False)
    if tipped.returncode != 0:
        return BaseDrift(False, fetched, 0, base_ref, "")
    return BaseDrift(True, fetched, int(counted.stdout.strip()), base_ref, tipped.stdout.strip())


def create_tracking(project_root: Path, worktree_dir: str, name: str, branch: str) -> Path:
    """Worktree checked out on an existing branch, fetching it if it is remote-only.

    The counterpart to `create()`: that one cuts a *new* branch for work we are
    about to do, this one mirrors a branch someone else already pushed (e.g. a
    PR a CI lane opened). Converges rather than duplicating — a worktree
    already on `branch`, wherever it lives, is returned untouched.
    """
    for wt in list_worktrees(project_root):
        if wt.branch == branch:
            return wt.path

    path = project_root / worktree_dir / name
    if path.exists():
        raise FileExistsError(
            f"Worktree {path} already exists but is not on {branch!r} — "
            f"remove it with `agent worktree remove {name}` before mirroring that branch."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    run(
        ["git", "fetch", "origin", branch],
        cwd=project_root,
        check=False,
        timeout=SLOW_GIT_TIMEOUT_S,
    )
    local_exists = (
        run(["git", "rev-parse", "--verify", f"refs/heads/{branch}"], cwd=project_root, check=False)
    ).returncode == 0
    cmd = (
        ["git", "worktree", "add", str(path), branch]
        if local_exists
        else ["git", "worktree", "add", "--track", "-b", branch, str(path), f"origin/{branch}"]
    )
    proc = run(cmd, cwd=project_root, check=False, timeout=SLOW_GIT_TIMEOUT_S)
    if proc.returncode != 0:
        raise CommandError(
            f"git worktree add failed for existing branch {branch!r}:\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return path


def create_detached(project_root: Path, worktree_dir: str, name: str, ref: str) -> Path:
    """Read-only style worktree pinned to a ref (no branch) — e.g. for triage."""
    path = project_root / worktree_dir / name
    if path.exists():
        raise FileExistsError(f"Worktree {path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    run(
        ["git", "worktree", "add", "--detach", str(path), ref],
        cwd=project_root,
        timeout=SLOW_GIT_TIMEOUT_S,
    )
    return path


def changed_paths(cwd: Path) -> list[str]:
    """Every path git currently reports as changed: staged, unstaged, and untracked.

    `-z` NUL-delimits entries so a path containing a space parses correctly,
    and porcelain (not `git diff --name-only`) is what sees untracked files
    AND a staged-but-uncommitted edit — a diff-only check compares working
    tree to index and is blind to the latter, which is exactly the gap that
    let a staged edit slip past a diff-based allowlist check undetected.
    """
    proc = run(["git", "status", "--porcelain", "-z"], cwd=cwd)
    entries = [e for e in proc.stdout.split("\0") if e]
    paths = []
    skip_next = False
    for entry in entries:
        if skip_next:
            skip_next = False  # a rename/copy's old path trails as its own entry
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if status[0] in "RC":
            skip_next = True
    return paths


def list_worktrees(project_root: Path) -> list[Worktree]:
    proc = run(["git", "worktree", "list", "--porcelain"], cwd=project_root)
    trees: list[Worktree] = []
    current_path: Path | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree "))
        elif line.startswith("branch ") and current_path is not None:
            branch = line.removeprefix("branch refs/heads/")
            trees.append(Worktree(current_path, branch))
            current_path = None
    return trees


def remove(
    project_root: Path,
    worktree_dir: str,
    task_id: str,
    *,
    force: bool = False,
    delete_branch: bool = False,
) -> None:
    path = project_root / worktree_dir / task_id
    branch = None
    if delete_branch:
        for wt in list_worktrees(project_root):
            if wt.path.resolve() == path.resolve():
                branch = wt.branch
                break

    cmd = ["git", "worktree", "remove", str(path)]
    if force:
        cmd.insert(3, "--force")
    run(cmd, cwd=project_root)

    if branch is not None:
        # Mirror the worktree removal's fail-safe-unless-forced behavior:
        # -d refuses to delete a branch with unmerged commits.
        try:
            run(["git", "branch", "-D" if force else "-d", branch], cwd=project_root)
        except CommandError as exc:
            raise CommandError(
                f"Worktree removed, but branch {branch!r} was kept: it has unmerged "
                f"commits. Delete it with `git branch -D {branch}` if you are sure."
            ) from exc
