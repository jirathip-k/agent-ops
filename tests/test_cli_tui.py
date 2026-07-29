"""Both TUI entry points (`agent` and `agent tui`) must turn an invalid
`tui.theme` into a clean CLI error, not an unhandled traceback (issue #248).
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from agent_ops import tui
from agent_ops.cli import app

runner = CliRunner()


def _boom(project_root: object) -> None:
    raise ValueError("tui.theme: 'catppucin-moka' is not a built-in theme. Valid themes: nord")


def test_bare_command_reports_invalid_theme_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tui, "run_tui", _boom)
    result = runner.invoke(app, [])
    assert result.exit_code == 1
    assert "catppucin-moka" in result.output


def test_tui_subcommand_reports_invalid_theme_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tui, "run_tui", _boom)
    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 1
    assert "catppucin-moka" in result.output
