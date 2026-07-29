"""Both TUI entry points (`agent` and `agent tui`) must turn a bad `tui.*`
config into a clean CLI error, not an unhandled traceback.

Two independent angles on the same guarantee, from #248 and #249 respectively:
an injected exception out of `run_tui` (an invalid `tui.theme`), and a real
`.agent/config.yaml` that pydantic refuses at load (an invalid
`tui.chat_sink`). The first pins the CLI's error handling in isolation; the
second pins the whole path from disk to message, which only became reachable
once `TuiApp.__init__` started loading project config eagerly.
"""

from __future__ import annotations

from pathlib import Path

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


# `.agent/config.yaml` with a `tui.chat_sink` template pydantic refuses at
# load (#249: must `shlex.split` cleanly and contain a `{text}` token).
INVALID_CONFIG = 'tui:\n  chat_sink: "mymux send --pane right"\n'


def _write_invalid_config(project: Path) -> None:
    agent_dir = project / ".agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "config.yaml").write_text(INVALID_CONFIG)


def test_tui_command_reports_a_bad_config_without_a_traceback(tmp_path: Path) -> None:
    """`TuiApp.__init__` loads the project config eagerly (#249) — before this
    fix, a config another command would cleanly refuse instead crashed `agent
    tui` with a raw pydantic traceback, where every other command reporting a
    bad config prints a message and exits 1."""
    _write_invalid_config(tmp_path)

    result = runner.invoke(app, ["tui", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "tui.chat_sink" in result.output


def test_bare_agent_reports_a_bad_config_without_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-subcommand path (`agent` alone) opens the same TUI and must
    fail the same friendly way, not just the explicit `agent tui` command."""
    _write_invalid_config(tmp_path)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [])

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "tui.chat_sink" in result.output
