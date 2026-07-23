from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from agent_ops.utils import CommandError, run


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str


def create(project_root: Path, worktree_dir: str, task_id: str, branch: str, base: str) -> Path:
    """Create an isolated worktree for one task, on a fresh branch cut from base."""
    path = project_root / worktree_dir / task_id
    if path.exists():
        raise FileExistsError(
            f"Worktree {path} already exists. Remove it with `agent worktree remove {task_id}`."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Branch from origin/<base> when it exists: always-fresh base, and it
    # sidesteps a git DWIM trap — with a remote-only base, `worktree add -b`
    # silently ignores -b and checks out a new local <base> branch instead.
    base_ref = base
    if run(["git", "fetch", "origin", base], cwd=project_root, check=False).returncode == 0:
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


def create_detached(project_root: Path, worktree_dir: str, name: str, ref: str) -> Path:
    """Read-only style worktree pinned to a ref (no branch) — e.g. for triage."""
    path = project_root / worktree_dir / name
    if path.exists():
        raise FileExistsError(f"Worktree {path} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "worktree", "add", "--detach", str(path), ref], cwd=project_root)
    return path


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
