from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops.cli import _checkout_drift, _missing_gitignore_markers, app
from agent_ops.utils import run

runner = CliRunner()


def _commit(repo: Path, message: str) -> None:
    (repo / f"{message}.txt").write_text(f"{message}\n")
    run(["git", "add", "."], cwd=repo)
    run(["git", "commit", "-m", message], cwd=repo)


@pytest.fixture()
def origin_and_clone(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    run(["git", "init", "-b", "main", "--bare"], cwd=origin)

    seed = tmp_path / "seed"
    seed.mkdir()
    run(["git", "init", "-b", "main"], cwd=seed)
    run(["git", "config", "user.email", "test@example.com"], cwd=seed)
    run(["git", "config", "user.name", "test"], cwd=seed)
    _commit(seed, "init")
    run(["git", "remote", "add", "origin", str(origin)], cwd=seed)
    run(["git", "push", "-u", "origin", "main"], cwd=seed)

    clone = tmp_path / "clone"
    run(["git", "clone", str(origin), str(clone)])
    run(["git", "config", "user.email", "test@example.com"], cwd=clone)
    run(["git", "config", "user.name", "test"], cwd=clone)
    return origin, clone


def _push_from_seed(tmp_path: Path, origin: Path) -> None:
    seed = tmp_path / "seed"
    _commit(seed, "upstream-change")
    run(["git", "push", "origin", "main"], cwd=seed)


def test_missing_gitignore_markers_both_present(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".worktrees/\n.agent-runs/\n")
    assert _missing_gitignore_markers(tmp_path) == []


def test_missing_gitignore_markers_only_worktrees_present(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".worktrees/\n")
    assert _missing_gitignore_markers(tmp_path) == [".agent-runs/"]


def test_missing_gitignore_markers_only_agent_runs_present(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text(".agent-runs/\n")
    assert _missing_gitignore_markers(tmp_path) == [".worktrees/"]


def test_missing_gitignore_markers_neither_present(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("dist/\n")
    assert _missing_gitignore_markers(tmp_path) == [".worktrees/", ".agent-runs/"]


def test_missing_gitignore_markers_no_gitignore_file(tmp_path: Path) -> None:
    assert _missing_gitignore_markers(tmp_path) == [".worktrees/", ".agent-runs/"]


def test_doctor_warns_on_missing_marker_but_exit_code_stays_zero(
    tmp_path: Path, monkeypatch
) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    (tmp_path / ".gitignore").write_text(".worktrees/\n")
    monkeypatch.setattr("agent_ops.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "! .gitignore missing .agent-runs/ — run: agent init" in result.output


def test_doctor_no_warning_when_both_markers_present(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    monkeypatch.setattr("agent_ops.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert ".gitignore missing" not in result.output


def test_doctor_warns_on_no_gitignore_file(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    (tmp_path / ".gitignore").unlink()
    monkeypatch.setattr("agent_ops.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "! .gitignore missing .worktrees/, .agent-runs/ — run: agent init" in result.output


def test_checkout_drift_in_sync(origin_and_clone: tuple[Path, Path]) -> None:
    _, clone = origin_and_clone
    assert _checkout_drift(clone) is None


def test_checkout_drift_behind(tmp_path: Path, origin_and_clone: tuple[Path, Path]) -> None:
    origin, clone = origin_and_clone
    _push_from_seed(tmp_path, origin)
    run(["git", "fetch"], cwd=clone)

    drift = _checkout_drift(clone)

    assert drift is not None
    assert "1 commit behind" in drift
    assert "origin/main" in drift
    assert str(clone) in drift
    assert "ahead" not in drift


def test_checkout_drift_ahead(origin_and_clone: tuple[Path, Path]) -> None:
    _, clone = origin_and_clone
    _commit(clone, "local-change")

    drift = _checkout_drift(clone)

    assert drift is not None
    assert "1 commit ahead of origin/main" in drift
    assert "behind" not in drift


def test_checkout_drift_diverged(tmp_path: Path, origin_and_clone: tuple[Path, Path]) -> None:
    origin, clone = origin_and_clone
    _push_from_seed(tmp_path, origin)
    run(["git", "fetch"], cwd=clone)
    _commit(clone, "local-change")

    drift = _checkout_drift(clone)

    assert drift is not None
    assert "diverged" in drift
    assert "1 commit ahead" in drift
    assert "1 commit behind" in drift


def test_checkout_drift_no_upstream(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-b", "main"], cwd=repo)
    run(["git", "config", "user.email", "test@example.com"], cwd=repo)
    run(["git", "config", "user.name", "test"], cwd=repo)
    _commit(repo, "init")

    assert _checkout_drift(repo) is None


def test_checkout_drift_detached_head(origin_and_clone: tuple[Path, Path]) -> None:
    _, clone = origin_and_clone
    run(["git", "checkout", "--detach"], cwd=clone)

    assert _checkout_drift(clone) is None


def test_checkout_drift_not_a_repo(tmp_path: Path) -> None:
    assert _checkout_drift(tmp_path) is None


def test_checkout_drift_never_fetches(
    tmp_path: Path, origin_and_clone: tuple[Path, Path], monkeypatch
) -> None:
    origin, clone = origin_and_clone
    _push_from_seed(tmp_path, origin)
    run(["git", "fetch"], cwd=clone)
    _commit(clone, "local-change")

    recorded: list[list[str]] = []
    real_run = run

    def recording_run(cmd, **kwargs):
        recorded.append(cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("agent_ops.cli.run", recording_run)

    _checkout_drift(clone)

    assert recorded, "expected _checkout_drift to invoke git"
    assert not any("fetch" in cmd for cmd in recorded)


def test_doctor_reports_checkout_drift(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    monkeypatch.setattr("agent_ops.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(
        "agent_ops.cli._checkout_drift",
        lambda root: "agent-ops checkout is 4 commits behind origin/main",
    )

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "! agent-ops checkout is 4 commits behind origin/main" in result.output


def test_doctor_silent_when_checkout_in_sync(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    monkeypatch.setattr("agent_ops.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr("agent_ops.cli._checkout_drift", lambda root: None)

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "agent-ops checkout" not in result.output
