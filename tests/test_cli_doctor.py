from pathlib import Path

from typer.testing import CliRunner

from agent_ops.cli import _missing_gitignore_markers, app

runner = CliRunner()


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
