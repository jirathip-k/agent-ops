from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops import surfaces, worktree
from agent_ops.cli import app
from agent_ops.utils import CommandError, run

runner = CliRunner()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    run(["git", "init", "-b", "main"], cwd=tmp_path)
    run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    run(["git", "config", "user.name", "test"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("hello\n")
    run(["git", "add", "."], cwd=tmp_path)
    run(["git", "commit", "-m", "init"], cwd=tmp_path)
    return tmp_path


class FakeSurface:
    name = "fake"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str], Path, Path | None]] = []

    def available(self) -> bool:
        return True

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> str:
        self.calls.append((label, command, cwd, attach_path))
        return "fake surface"


class FailingSurface:
    name = "failing"

    def available(self) -> bool:
        return True

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> str:
        raise CommandError("spawn exploded")


def test_dispatch_precreates_worktree_and_attaches_surface_to_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(app, ["dispatch", "5", "--project", str(repo)])

    assert result.exit_code == 0
    root = repo.resolve()
    wt_path = root / ".worktrees" / "issue-5"
    assert wt_path.is_dir()
    assert "fix/issue-5" in [wt.branch for wt in worktree.list_worktrees(repo)]
    ((label, command, cwd, attach_path),) = fake.calls
    assert label == "agent-issue-5"
    assert command[:3] == ["agent", "implement", "5"]
    assert cwd == root  # logs and process stay at the project root
    assert attach_path == wt_path  # the visible run lives under the issue's card


def test_dispatch_cleans_up_worktree_and_branch_when_spawn_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": FailingSurface())

    result = runner.invoke(app, ["dispatch", "6", "--project", str(repo)])

    assert result.exit_code == 1
    assert not (repo / ".worktrees" / "issue-6").exists()
    branches = run(["git", "branch", "--list", "fix/issue-6"], cwd=repo).stdout
    assert "fix/issue-6" not in branches
    # cleanup worked, so an immediate retry can create the worktree again
    retry_fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": retry_fake)
    assert runner.invoke(app, ["dispatch", "6", "--project", str(repo)]).exit_code == 0


def test_dispatch_fails_when_worktree_already_exists(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree.create(repo, ".worktrees", "issue-7", "fix/issue-7", "main")
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(app, ["dispatch", "7", "--project", str(repo)])

    assert result.exit_code == 1
    assert fake.calls == []  # never spawned onto a leftover worktree
