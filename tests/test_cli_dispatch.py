import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops import github, messages, surfaces, worktree
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
    ) -> surfaces.Spawned:
        self.calls.append((label, command, cwd, attach_path))
        return surfaces.Spawned(where="fake surface", surface=self.name)


class FailingSurface:
    name = "failing"

    def available(self) -> bool:
        return True

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> surfaces.Spawned:
        raise CommandError("spawn exploded")


class WorktreeEatingSurface:
    """Reports success while leaving nothing behind for dispatch to attach to —
    e.g. a terminal whose worktree was removed concurrently, or a fake Orca
    card pointed at a path that never existed (issue #201)."""

    name = "orca"

    def available(self) -> bool:
        return True

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> surfaces.Spawned:
        if attach_path is not None:
            shutil.rmtree(attach_path, ignore_errors=True)
        return surfaces.Spawned(
            where=f"orca terminal {label!r} (handle term_ghost)",
            surface=self.name,
            handle="term_ghost",
        )


class WarningSurface:
    name = "orca"

    def available(self) -> bool:
        return True

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> surfaces.Spawned:
        return surfaces.Spawned(
            where=f"orca terminal {label!r} (handle term_abc)",
            surface=self.name,
            handle="term_abc",
            warning="fell back to project root card — shell starts at the project root",
        )


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


def test_dispatch_keeps_worktree_and_branch_when_spawn_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": FailingSurface())

    result = runner.invoke(app, ["dispatch", "6", "--project", str(repo)])

    assert result.exit_code == 1
    # the surface attach failed, not the worktree — keep both so a retry can reuse them
    assert (repo / ".worktrees" / "issue-6").is_dir()
    branches = run(["git", "branch", "--list", "fix/issue-6"], cwd=repo).stdout
    assert "fix/issue-6" in branches
    stderr = result.stderr.lower()
    assert "spawn exploded" in stderr
    assert "kept" in stderr


def test_dispatch_retry_after_spawn_failure_reuses_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": FailingSurface())
    first = runner.invoke(app, ["dispatch", "6", "--project", str(repo)])
    assert first.exit_code == 1

    # retry re-runs dispatch's worktree.create with reuse=True: it must reuse
    # the pristine leftover worktree instead of raising FileExistsError
    retry_fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": retry_fake)
    second = runner.invoke(app, ["dispatch", "6", "--project", str(repo)])

    assert second.exit_code == 0
    root = repo.resolve()
    wt_path = root / ".worktrees" / "issue-6"
    ((_, _, _, attach_path),) = retry_fake.calls
    assert attach_path == wt_path


def test_dispatch_fails_when_worktree_already_exists(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree.create(repo, ".worktrees", "issue-7", "fix/issue-7", "main")
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(app, ["dispatch", "7", "--project", str(repo)])

    # pristine leftover matching the issue's branch is reused (this is the
    # retry path), so this now succeeds by attaching to the existing worktree
    assert result.exit_code == 0
    ((_, _, _, attach_path),) = fake.calls
    assert attach_path == repo.resolve() / ".worktrees" / "issue-7"


def test_dispatch_fails_fast_on_dirty_leftover_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree.create(repo, ".worktrees", "issue-8", "fix/issue-8", "main")
    (repo / ".worktrees" / "issue-8" / "dirty.txt").write_text("oops\n")
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(app, ["dispatch", "8", "--project", str(repo)])

    assert result.exit_code == 1
    assert fake.calls == []  # never spawned onto a leftover, dirty worktree


def test_dispatch_worktree_creation_failure_leaves_nothing_behind(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    def failing_create(*args: object, **kwargs: object) -> Path:
        raise CommandError("git worktree add failed: simulated failure")

    monkeypatch.setattr(worktree, "create", failing_create)

    result = runner.invoke(app, ["dispatch", "9", "--project", str(repo)])

    assert result.exit_code == 1
    assert not (repo / ".worktrees" / "issue-9").exists()
    branches = run(["git", "branch", "--list", "fix/issue-9"], cwd=repo).stdout
    assert "fix/issue-9" not in branches
    assert fake.calls == []  # never spawned when the worktree itself failed


def test_dispatch_refuses_when_issue_already_has_open_pr(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)
    monkeypatch.setattr(
        github,
        "open_prs_for_issue",
        lambda number, cwd: [
            {"number": 141, "url": "https://github.com/org/repo/pull/141", "headRefName": ""}
        ],
    )

    result = runner.invoke(app, ["dispatch", "8", "--project", str(repo)])

    assert result.exit_code == 1
    assert "#141" in result.output
    assert not (repo / ".worktrees" / "issue-8").exists()
    assert fake.calls == []  # never spawned onto a duplicate


def test_dispatch_force_bypasses_open_pr_guard_and_forwards_flag(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)
    monkeypatch.setattr(
        github,
        "open_prs_for_issue",
        lambda number, cwd: [
            {"number": 141, "url": "https://github.com/org/repo/pull/141", "headRefName": ""}
        ],
    )

    result = runner.invoke(app, ["dispatch", "9", "--project", str(repo), "--force"])

    assert result.exit_code == 0
    ((_, command, _, _),) = fake.calls
    assert "--force" in command


def test_dispatch_forwards_plan_file_as_absolute_path(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative --plan-file must reach implement absolutized: the surface may
    spawn from the worktree, where a caller-relative path resolves wrong."""
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)
    plan = tmp_path / "plan.md"
    plan.write_text("approved plan")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["dispatch", "5", "--project", str(repo), "--plan-file", "plan.md"])

    assert result.exit_code == 0
    ((_, command, _, _),) = fake.calls
    assert "--plan-file" in command
    assert command[command.index("--plan-file") + 1] == str(plan.resolve())


def test_dispatch_rejects_missing_plan_file_before_creating_a_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(app, ["dispatch", "5", "--project", str(repo), "--plan-file", "nope.md"])

    assert result.exit_code == 1
    assert not (repo.resolve() / ".worktrees" / "issue-5").exists()
    assert fake.calls == []


def test_dispatch_forwards_grant_file_as_absolute_path(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative --grant-file must reach implement absolutized, exactly like
    --plan-file — the surface may spawn from the worktree, not the caller's cwd."""
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)
    grant = tmp_path / "grant.yaml"
    grant.write_text("issue: 5\ngranted_by: x\nscope: s\npaths: [a]\n")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(
        app, ["dispatch", "5", "--project", str(repo), "--grant-file", "grant.yaml"]
    )

    assert result.exit_code == 0
    ((_, command, _, _),) = fake.calls
    assert "--grant-file" in command
    assert command[command.index("--grant-file") + 1] == str(grant.resolve())


def test_dispatch_rejects_missing_grant_file_before_creating_a_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeSurface()
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)

    result = runner.invoke(
        app, ["dispatch", "5", "--project", str(repo), "--grant-file", "nope.yaml"]
    )

    assert result.exit_code == 1
    assert not (repo.resolve() / ".worktrees" / "issue-5").exists()
    assert fake.calls == []


def test_dispatch_records_how_to_reach_the_run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #98: the terminal handle is a dispatched run's one stable
    identity, and it used to be formatted into prose and dropped."""

    class OrcaLikeSurface:
        name = "orca"

        def available(self) -> bool:
            return True

        def spawn(
            self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
        ) -> surfaces.Spawned:
            return surfaces.Spawned(where="w", surface="orca", handle="term_abc")

    monkeypatch.setattr(surfaces, "pick", lambda name="auto": OrcaLikeSurface())

    result = runner.invoke(app, ["dispatch", "5", "--project", str(repo)])

    assert result.exit_code == 0
    record = messages.load_spawn(repo.resolve(), 5)
    assert record is not None
    assert record.handle == "term_abc"


def test_dispatch_records_a_handleless_surface_without_inventing_a_channel(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background surface has no message bus. The record still describes
    the run — pid and log — but claims no handle, so nothing tries to push."""

    class BackgroundLikeSurface:
        name = "background"

        def available(self) -> bool:
            return True

        def spawn(
            self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
        ) -> surfaces.Spawned:
            return surfaces.Spawned(
                where="w", surface="background", pid=999, log_path=cwd / ".agent-runs" / "x.log"
            )

    monkeypatch.setattr(surfaces, "pick", lambda name="auto": BackgroundLikeSurface())

    result = runner.invoke(app, ["dispatch", "5", "--project", str(repo)])

    assert result.exit_code == 0
    record = messages.load_spawn(repo.resolve(), 5)
    assert record is not None
    assert record.handle is None
    assert record.pid == 999


def test_dispatch_records_nothing_when_the_spawn_failed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No run started, so there is no mailbox to point a supervisor at."""
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": FailingSurface())

    result = runner.invoke(app, ["dispatch", "6", "--project", str(repo)])

    assert result.exit_code == 1
    assert messages.load_spawn(repo.resolve(), 6) is None


def test_dispatch_fails_when_the_surface_reports_success_over_a_dead_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #201: dispatch must verify its own postcondition rather than
    trusting the surface's report — a surface can say "spawned" while the
    worktree it should be running in is gone (e.g. removed concurrently)."""
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": WorktreeEatingSurface())

    result = runner.invoke(app, ["dispatch", "10", "--project", str(repo)])

    assert result.exit_code == 1
    stderr = result.stderr.lower()
    assert "term_ghost" in stderr  # names what was created, so it can be closed by hand
    assert "did not produce a worktree" in stderr
    assert "dispatched issue" not in result.output  # no false success line
    # the spawn record is still written — it is the only address the
    # orphaned terminal has once the worktree itself is gone
    record = messages.load_spawn(repo.resolve(), 10)
    assert record is not None
    assert record.handle == "term_ghost"


def test_dispatch_prints_a_warning_and_still_succeeds(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded-but-successful spawn (e.g. Orca's project-root fallback)
    must still report success, but the degradation must not be silent."""
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": WarningSurface())

    result = runner.invoke(app, ["dispatch", "11", "--project", str(repo)])

    assert result.exit_code == 0
    assert "dispatched issue #11" in result.output
    assert "fell back to project root card" in result.stderr
