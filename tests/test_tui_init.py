"""`run_tui`'s theme resolution (issue #248): default, project override, and
the "fail at startup, not silently" contract for an unknown theme name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_ops.tui import run_tui


class _FakeApp:
    """Stands in for `TuiApp` — captures the theme it was constructed with
    without opening a real terminal session."""

    last_theme: str | None = None

    def __init__(self, project_root: Path, *, theme: str) -> None:
        _FakeApp.last_theme = theme

    def run(self) -> None:
        return None


def _write_config(tmp_path: Path, body: str) -> None:
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir(exist_ok=True)
    (agent_dir / "config.yaml").write_text(body)


def test_defaults_to_catppuccin_macchiato_with_no_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("agent_ops.tui.app.TuiApp", _FakeApp)
    run_tui(tmp_path)
    assert _FakeApp.last_theme == "catppuccin-macchiato"


def test_project_config_selects_a_built_in_theme(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_config(tmp_path, "tui:\n  theme: nord\n")
    monkeypatch.setattr("agent_ops.tui.app.TuiApp", _FakeApp)
    run_tui(tmp_path)
    assert _FakeApp.last_theme == "nord"


def test_unknown_theme_raises_before_the_app_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_config(tmp_path, "tui:\n  theme: catppucin-moka\n")

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("TuiApp must not be constructed for an invalid theme")

    monkeypatch.setattr("agent_ops.tui.app.TuiApp", _boom)

    with pytest.raises(ValueError) as excinfo:
        run_tui(tmp_path)
    message = str(excinfo.value)
    assert "catppucin-moka" in message
    assert "catppuccin-macchiato" in message
