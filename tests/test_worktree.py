import shutil
import subprocess
from pathlib import Path

import pytest

from agent_ops import worktree
from agent_ops.utils import TIMEOUT_RETURNCODE, CommandError, run


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


def test_create_with_remote_only_base_stays_on_task_branch(tmp_path: Path) -> None:
    """git DWIM regression: remote-only base must not hijack the -b branch."""
    origin = tmp_path / "origin"
    origin.mkdir()
    run(["git", "init", "-b", "main"], cwd=origin)
    run(["git", "config", "user.email", "t@example.com"], cwd=origin)
    run(["git", "config", "user.name", "t"], cwd=origin)
    (origin / "f.txt").write_text("hi\n")
    run(["git", "add", "."], cwd=origin)
    run(["git", "commit", "-m", "init"], cwd=origin)
    run(["git", "branch", "staging"], cwd=origin)

    clone = tmp_path / "clone"
    run(["git", "clone", str(origin), str(clone)], cwd=tmp_path)
    # no local staging branch here — only origin/staging

    path = worktree.create(clone, ".worktrees", "issue-9", "fix/issue-9", "staging")
    proc = run(["git", "branch", "--show-current"], cwd=path)
    assert proc.stdout.strip() == "fix/issue-9"


def _clone_with_remote_branch(tmp_path: Path, branch: str) -> tuple[Path, Path]:
    """(origin, clone) where `branch` exists on the origin only."""
    origin = tmp_path / "origin"
    origin.mkdir()
    run(["git", "init", "-b", "main"], cwd=origin)
    run(["git", "config", "user.email", "t@example.com"], cwd=origin)
    run(["git", "config", "user.name", "t"], cwd=origin)
    (origin / "f.txt").write_text("hi\n")
    run(["git", "add", "."], cwd=origin)
    run(["git", "commit", "-m", "init"], cwd=origin)
    run(["git", "branch", branch], cwd=origin)

    clone = tmp_path / "clone"
    run(["git", "clone", str(origin), str(clone)], cwd=tmp_path)
    return origin, clone


def test_create_tracking_checks_out_a_remote_only_branch(tmp_path: Path) -> None:
    _origin, clone = _clone_with_remote_branch(tmp_path, "fix/issue-4")

    path = worktree.create_tracking(clone, ".worktrees", "issue-4", "fix/issue-4")

    assert path == clone / ".worktrees" / "issue-4"
    assert run(["git", "branch", "--show-current"], cwd=path).stdout.strip() == "fix/issue-4"


def test_create_tracking_is_idempotent(tmp_path: Path) -> None:
    _origin, clone = _clone_with_remote_branch(tmp_path, "fix/issue-4")
    first = worktree.create_tracking(clone, ".worktrees", "issue-4", "fix/issue-4")

    again = worktree.create_tracking(clone, ".worktrees", "issue-4", "fix/issue-4")

    assert again == first
    branches = [wt.branch for wt in worktree.list_worktrees(clone)]
    assert branches.count("fix/issue-4") == 1


def test_create_tracking_reuses_a_worktree_under_another_name(tmp_path: Path) -> None:
    # `agent dispatch` may already have that branch checked out elsewhere; the
    # viewer must adopt it rather than fail or duplicate it.
    _origin, clone = _clone_with_remote_branch(tmp_path, "fix/issue-4")
    existing = worktree.create_tracking(clone, ".worktrees", "dispatched", "fix/issue-4")

    adopted = worktree.create_tracking(clone, ".worktrees", "issue-4", "fix/issue-4")

    assert adopted == existing


def test_create_tracking_refuses_a_leftover_directory_on_another_branch(repo: Path) -> None:
    worktree.create(repo, ".worktrees", "issue-4", "other/branch", "main")
    run(["git", "branch", "fix/issue-4"], cwd=repo)

    with pytest.raises(FileExistsError, match="not on 'fix/issue-4'"):
        worktree.create_tracking(repo, ".worktrees", "issue-4", "fix/issue-4")


def test_create_tracking_raises_when_the_branch_does_not_exist(repo: Path) -> None:
    with pytest.raises(CommandError, match="git worktree add failed"):
        worktree.create_tracking(repo, ".worktrees", "issue-99", "fix/issue-99")


def test_remote_branch_status_reports_neither_when_branch_is_unknown(repo: Path) -> None:
    status = worktree.remote_branch_status(repo, "fix/issue-none")

    assert status.local is False
    assert status.remote is False
    assert status.diverged is False


def test_remote_branch_status_reports_local_only(repo: Path) -> None:
    run(["git", "branch", "fix/issue-local"], cwd=repo)

    status = worktree.remote_branch_status(repo, "fix/issue-local")

    assert status.local is True
    assert status.remote is False
    assert status.ahead == 0
    assert status.behind == 0


def test_remote_branch_status_reports_ahead_behind(tmp_path: Path) -> None:
    origin, clone = _clone_with_remote_branch(tmp_path, "fix/issue-4")
    run(["git", "branch", "fix/issue-4", "origin/fix/issue-4"], cwd=clone)
    run(["git", "checkout", "fix/issue-4"], cwd=origin)
    (origin / "extra.txt").write_text("more\n")
    run(["git", "add", "."], cwd=origin)
    run(["git", "commit", "-m", "origin moves on"], cwd=origin)

    status = worktree.remote_branch_status(clone, "fix/issue-4")

    assert status.local is True
    assert status.remote is True
    assert status.ahead == 0
    assert status.behind == 1
    assert status.diverged is False


def test_prune_drops_a_stale_registration(repo: Path) -> None:
    path = worktree.create(repo, ".worktrees", "issue-13", "fix/issue-13", "main")
    shutil.rmtree(path)

    worktree.prune(repo)

    branches = [wt.branch for wt in worktree.list_worktrees(repo)]
    assert "fix/issue-13" not in branches


def test_create_twice_fails(repo: Path) -> None:
    worktree.create(repo, ".worktrees", "issue-2", "fix/issue-2", "main")
    with pytest.raises(FileExistsError):
        worktree.create(repo, ".worktrees", "issue-2", "fix/issue-2", "main")


def test_create_reuse_returns_pristine_precreated_worktree(repo: Path) -> None:
    path = worktree.create(repo, ".worktrees", "issue-10", "fix/issue-10", "main")
    again = worktree.create(repo, ".worktrees", "issue-10", "fix/issue-10", "main", reuse=True)
    assert again == path


def test_create_reuse_rejects_dirty_worktree(repo: Path) -> None:
    path = worktree.create(repo, ".worktrees", "issue-11", "fix/issue-11", "main")
    (path / "dirty.txt").write_text("uncommitted\n")
    with pytest.raises(FileExistsError):
        worktree.create(repo, ".worktrees", "issue-11", "fix/issue-11", "main", reuse=True)


def test_create_reuse_rejects_wrong_branch(repo: Path) -> None:
    path = worktree.create(repo, ".worktrees", "issue-12", "fix/issue-12", "main")
    run(["git", "checkout", "-b", "other-branch"], cwd=path)
    with pytest.raises(FileExistsError):
        worktree.create(repo, ".worktrees", "issue-12", "fix/issue-12", "main", reuse=True)


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


def _clone(tmp_path: Path) -> tuple[Path, Path]:
    """(origin, clone) — a plain clone, so `origin/main` tracks the default branch."""
    origin = tmp_path / "origin"
    origin.mkdir()
    run(["git", "init", "-b", "main"], cwd=origin)
    run(["git", "config", "user.email", "t@example.com"], cwd=origin)
    run(["git", "config", "user.name", "t"], cwd=origin)
    (origin / "f.txt").write_text("hi\n")
    run(["git", "add", "."], cwd=origin)
    run(["git", "commit", "-m", "init"], cwd=origin)

    clone = tmp_path / "clone"
    run(["git", "clone", str(origin), str(clone)], cwd=tmp_path)
    return origin, clone


def test_base_drift_no_remote_ref_is_unchecked(repo: Path) -> None:
    """No `origin/<base>` at all (test/scratch repos) — unknown, not current."""
    drift = worktree.base_drift(repo, "main")

    assert drift.checked is False
    assert drift.behind == 0


def test_base_drift_current_base_is_zero_behind(tmp_path: Path) -> None:
    _origin, clone = _clone(tmp_path)

    drift = worktree.base_drift(clone, "main")

    assert drift.checked is True
    assert drift.fetched is True
    assert drift.behind == 0
    assert drift.base_ref == "origin/main"


def test_base_drift_reports_how_far_behind(tmp_path: Path) -> None:
    origin, clone = _clone(tmp_path)
    (origin / "later.txt").write_text("more work\n")
    run(["git", "add", "."], cwd=origin)
    run(["git", "commit", "-m", "later commit"], cwd=origin)

    drift = worktree.base_drift(clone, "main")

    assert drift.checked is True
    assert drift.behind == 1
    assert "later commit" in drift.tip


def test_base_drift_degrades_when_the_remote_cannot_be_reached(tmp_path: Path) -> None:
    """A dead `origin` URL must not raise — fall back to the last-known local ref."""
    origin, clone = _clone(tmp_path)
    (origin / "later.txt").write_text("more work\n")
    run(["git", "add", "."], cwd=origin)
    run(["git", "commit", "-m", "later commit"], cwd=origin)
    run(["git", "fetch", "origin"], cwd=clone)  # last-known origin/main now includes it
    run(["git", "remote", "set-url", "origin", str(tmp_path / "does-not-exist")], cwd=clone)

    drift = worktree.base_drift(clone, "main")

    assert drift.checked is True
    assert drift.fetched is False
    assert drift.behind == 1


def test_base_drift_falls_back_to_local_ref_when_the_fetch_hangs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung fetch must degrade the same as a fast-failing one.

    `run(check=False)` reports a timeout as a synthetic `CompletedProcess`
    with `returncode == TIMEOUT_RETURNCODE` rather than raising — so the
    fetch here never raises, and `base_drift` must fall through to the
    last-known local `origin/main` exactly as
    `test_base_drift_degrades_when_the_remote_cannot_be_reached` does.
    """
    origin, clone = _clone(tmp_path)
    (origin / "later.txt").write_text("more work\n")
    run(["git", "add", "."], cwd=origin)
    run(["git", "commit", "-m", "later commit"], cwd=origin)
    run(["git", "fetch", "origin"], cwd=clone)  # last-known origin/main now includes it

    real_run = worktree.run

    def hung_fetch(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(
                cmd, TIMEOUT_RETURNCODE, stdout="", stderr="did not finish within 600s"
            )
        return real_run(cmd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worktree, "run", hung_fetch)

    drift = worktree.base_drift(clone, "main")

    assert drift.checked is True
    assert drift.fetched is False
    assert drift.behind == 1


def test_changed_paths_sees_untracked_and_staged_files(repo: Path) -> None:
    """`git diff --name-only` compares working tree to index — it misses a
    staged edit, since index and working tree already agree. A `git commit`
    then commits the whole index regardless, so an allowlist check built on
    `git diff` alone would let a staged edit outside it slip through
    undetected. `changed_paths` uses `git status --porcelain` instead, which
    sees a staged change and an untracked file alike.
    """
    (repo / "untracked.md").write_text("new file\n")
    (repo / "README.md").write_text("staged edit\n")
    run(["git", "add", "README.md"], cwd=repo)

    diff_only = run(["git", "diff", "--name-only"], cwd=repo).stdout.splitlines()
    assert "README.md" not in diff_only

    assert set(worktree.changed_paths(repo)) == {"README.md", "untracked.md"}
