import subprocess
from pathlib import Path

import pytest

from agent_ops import orca


def test_executable_honors_orca_cli_command_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCA_CLI_COMMAND", "orca-dev")
    assert orca.executable() == "orca-dev"


def test_report_updates_worktree_card(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", fake_run)
    orca.report(Path("/wt"), comment="planning", status=orca.STATUS_IN_PROGRESS)
    (cmd,) = calls
    assert cmd[1:3] == ["worktree", "set"]
    assert "path:/wt" in cmd
    assert "planning" in cmd
    assert "in-progress" in cmd


def test_report_is_a_noop_without_orca(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("must not invoke the CLI when Orca is unavailable")

    monkeypatch.setattr(orca, "available", lambda: False)
    monkeypatch.setattr(orca, "run", boom)
    orca.report(Path("/wt"), comment="planning")


def test_report_with_nothing_to_say_skips_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("must not invoke the CLI with no comment and no status")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", boom)
    orca.report(Path("/wt"))
