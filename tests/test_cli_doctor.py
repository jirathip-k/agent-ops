from pathlib import Path

from typer.testing import CliRunner

from agent_ops import stubs
from agent_ops.cli import _missing_gitignore_markers, app

runner = CliRunner()

IN_SYNC_TRIAGE_CALLER = """
jobs:
  triage:
    permissions:
      contents: write
      issues: write
      pull-requests: write
      id-token: write
      checks: read
      statuses: read
      actions: read
    uses: acme/agent-ops/.github/workflows/triage-pipeline.yml@main
    with:
      target_repo: ${{ github.repository }}
      max_issues: 3
      auto_merge: false
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      AGENT_APP_ID: ${{ secrets.AGENT_APP_ID }}
      AGENT_APP_PRIVATE_KEY: ${{ secrets.AGENT_APP_PRIVATE_KEY }}
"""


def _write_triage_caller(root: Path, text: str) -> None:
    workflows = root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "triage.yml").write_text(text)


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


def test_doctor_no_triage_output_when_caller_in_sync(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _write_triage_caller(tmp_path, IN_SYNC_TRIAGE_CALLER)
    monkeypatch.setattr("agent_ops.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "triage.yml" not in result.output


def test_doctor_warns_on_missing_secrets(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    caller = IN_SYNC_TRIAGE_CALLER.replace(
        "      AGENT_APP_ID: ${{ secrets.AGENT_APP_ID }}\n"
        "      AGENT_APP_PRIVATE_KEY: ${{ secrets.AGENT_APP_PRIVATE_KEY }}\n",
        "",
    )
    _write_triage_caller(tmp_path, caller)
    monkeypatch.setattr("agent_ops.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert (
        "! .github/workflows/triage.yml is behind stubs/managed-repo-triage.yml — "
        "missing secrets: AGENT_APP_ID, AGENT_APP_PRIVATE_KEY" in result.output
    )


def test_doctor_warns_on_missing_permission(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    caller = IN_SYNC_TRIAGE_CALLER.replace("      actions: read\n", "")
    _write_triage_caller(tmp_path, caller)
    monkeypatch.setattr("agent_ops.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "missing permissions: actions" in result.output


def test_doctor_no_warning_for_customised_triage_values(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    caller = IN_SYNC_TRIAGE_CALLER.replace("max_issues: 3", "max_issues: 10").replace(
        "auto_merge: false", "auto_merge: true\n      runner: blacksmith-2vcpu-ubuntu-2404"
    )
    _write_triage_caller(tmp_path, caller)
    monkeypatch.setattr("agent_ops.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "triage.yml" not in result.output


def test_doctor_no_output_when_no_triage_yml(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    monkeypatch.setattr("agent_ops.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "triage.yml" not in result.output


def test_doctor_reports_missing_stub_without_traceback(tmp_path: Path, monkeypatch) -> None:
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    _write_triage_caller(tmp_path, IN_SYNC_TRIAGE_CALLER)
    monkeypatch.setattr("agent_ops.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(stubs, "TRIAGE_STUB", tmp_path / "nonexistent-stub.yml")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "! triage.yml drift check skipped:" in result.output


def test_doctor_drift_message_follows_a_patched_stub(tmp_path: Path, monkeypatch) -> None:
    """The stub path in the warning must come from stubs.TRIAGE_STUB at call time.

    A by-value `from ... import TRIAGE_STUB` would silently keep reporting the
    real stub, and an unguarded relative_to() would raise for a stub outside
    PLATFORM_ROOT.
    """
    runner.invoke(app, ["init", "--project", str(tmp_path)])
    caller = IN_SYNC_TRIAGE_CALLER.replace("      actions: read\n", "")
    _write_triage_caller(tmp_path, caller)

    alt_stub = tmp_path / "alt-stub.yml"
    alt_stub.write_text(IN_SYNC_TRIAGE_CALLER)
    monkeypatch.setattr(stubs, "TRIAGE_STUB", alt_stub)
    monkeypatch.setattr("agent_ops.cli.shutil.which", lambda tool: f"/usr/bin/{tool}")

    result = runner.invoke(app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "alt-stub.yml" in result.output
    assert "missing permissions: actions" in result.output
