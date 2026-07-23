from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_ops.utils import run


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
    run(["git", "worktree", "add", "-b", branch, str(path), base], cwd=project_root)
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


def remove(project_root: Path, worktree_dir: str, task_id: str, *, force: bool = False) -> None:
    path = project_root / worktree_dir / task_id
    cmd = ["git", "worktree", "remove", str(path)]
    if force:
        cmd.insert(3, "--force")
    run(cmd, cwd=project_root)
