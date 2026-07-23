from pathlib import Path

import pytest

from agent_ops import worktree
from agent_ops.utils import CommandError, run


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    run(["git", "init", "-b", "main"], cwd=tmp_path)
    run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    run(["git", "config", "user.name", "test"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("hello\n")
    run(["git", "add", "."], cwd=tmp_path)
    run(["git", "commit", "-m", "init"], cwd=tmp_path)
    return tmp_path


def test_create_list_remove(repo: Path) -> None:
    path = worktree.create(repo, ".worktrees", "issue-1", "fix/issue-1", "main")
    assert path.is_dir()
    assert (path / "README.md").exists()

    branches = [wt.branch for wt in worktree.list_worktrees(repo)]
    assert "fix/issue-1" in branches

    worktree.remove(repo, ".worktrees", "issue-1")
    assert not path.exists()


def test_create_twice_fails(repo: Path) -> None:
    worktree.create(repo, ".worktrees", "issue-2", "fix/issue-2", "main")
    with pytest.raises(FileExistsError):
        worktree.create(repo, ".worktrees", "issue-2", "fix/issue-2", "main")


def test_remove_dirty_requires_force(repo: Path) -> None:
    path = worktree.create(repo, ".worktrees", "issue-3", "fix/issue-3", "main")
    (path / "dirty.txt").write_text("uncommitted\n")
    with pytest.raises(CommandError):
        worktree.remove(repo, ".worktrees", "issue-3")
    worktree.remove(repo, ".worktrees", "issue-3", force=True)
    assert not path.exists()


def test_remove_delete_branch(repo: Path) -> None:
    path = worktree.create(repo, ".worktrees", "issue-4", "fix/issue-4", "main")
    (path / "change.txt").write_text("unmerged change\n")
    run(["git", "add", "."], cwd=path)
    run(["git", "commit", "-m", "unmerged"], cwd=path)

    worktree.remove(repo, ".worktrees", "issue-4", force=True, delete_branch=True)

    assert not path.exists()
    branches = run(["git", "branch", "--list", "fix/issue-4"], cwd=repo).stdout
    assert "fix/issue-4" not in branches


def test_remove_without_delete_branch_keeps_branch(repo: Path) -> None:
    path = worktree.create(repo, ".worktrees", "issue-5", "fix/issue-5", "main")

    worktree.remove(repo, ".worktrees", "issue-5")

    assert not path.exists()
    branches = run(["git", "branch", "--list", "fix/issue-5"], cwd=repo).stdout
    assert "fix/issue-5" in branches


def test_delete_branch_without_force_keeps_unmerged_branch(repo: Path) -> None:
    path = worktree.create(repo, ".worktrees", "issue-6", "fix/issue-6", "main")
    (path / "change.txt").write_text("unmerged change\n")
    run(["git", "add", "."], cwd=path)
    run(["git", "commit", "-m", "unmerged"], cwd=path)

    with pytest.raises(CommandError, match="unmerged"):
        worktree.remove(repo, ".worktrees", "issue-6", delete_branch=True)

    # worktree is gone, but the unmerged branch survives
    assert not path.exists()
    branches = run(["git", "branch", "--list", "fix/issue-6"], cwd=repo).stdout
    assert "fix/issue-6" in branches


def test_delete_branch_without_force_deletes_merged_branch(repo: Path) -> None:
    path = worktree.create(repo, ".worktrees", "issue-7", "fix/issue-7", "main")

    worktree.remove(repo, ".worktrees", "issue-7", delete_branch=True)

    assert not path.exists()
    branches = run(["git", "branch", "--list", "fix/issue-7"], cwd=repo).stdout
    assert "fix/issue-7" not in branches
