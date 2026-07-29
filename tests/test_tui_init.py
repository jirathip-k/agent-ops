"""`run_tui`'s theme resolution (issue #248): default, project override, and
the "fail at startup, not silently" contract for an unknown theme name.

Also covers issue #255's cleanup: `run_tui` loads the project config once and
passes it into `TuiApp`, rather than the app re-reading it from disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import agent_ops.tui as tui_module
from agent_ops.config import load_project_config
from agent_ops.tui import run_tui


class _FakeApp:
    """Stands in for `TuiApp` — captures the theme and project config it was
    constructed with, without opening a real terminal session."""

    last_theme: str | None = None
    last_project_config: Any = None
    # `run_tui` reads this off the app after `run()` returns (issue #252) —
    # this stand-in needs it too, or attribute access blows up.
    chat_sink_error: str | None = None

    def __init__(self, project_root: Path, *, theme: str, project_config: Any = None) -> None:
        _FakeApp.last_theme = theme
        _FakeApp.last_project_config = project_config

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


def test_run_tui_loads_the_project_config_exactly_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`run_tui` used to load the config once for the theme and `TuiApp`
    loaded it again from disk — two reads (and two YAML parses) per `agent
    tui` startup, and a window for the two loads to disagree if the file
    changed in between (issue #255). Now there is exactly one load, shared
    with the app via the constructor."""
    monkeypatch.setattr("agent_ops.tui.app.TuiApp", _FakeApp)
    calls = 0
    real_loader = load_project_config

    def counting_loader(project_root: Path) -> Any:
        nonlocal calls
        calls += 1
        return real_loader(project_root)

    monkeypatch.setattr(tui_module, "load_project_config", counting_loader)

    run_tui(tmp_path)

    assert calls == 1


def test_run_tui_passes_the_config_instance_it_loaded_to_the_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("agent_ops.tui.app.TuiApp", _FakeApp)
    sentinel = load_project_config(tmp_path)
    monkeypatch.setattr(tui_module, "load_project_config", lambda project_root: sentinel)

    run_tui(tmp_path)

    assert _FakeApp.last_project_config is sentinel


def test_tui_app_uses_the_passed_project_config_without_reading_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`TuiApp` must use a passed-in `project_config` as-is rather than
    reloading it — this is the other half of issue #255's single-load fix."""
    from agent_ops import github
    from agent_ops.tui.app import TuiApp

    monkeypatch.setattr(github, "remote_slug", lambda root: None)

    def _boom(project_root: Path) -> Any:
        raise AssertionError("TuiApp must not reload project config when one is passed in")

    monkeypatch.setattr("agent_ops.tui.app.load_project_config", _boom)
    cfg = load_project_config(tmp_path)

    app = TuiApp(tmp_path, project_config=cfg)

    assert app.project_config is cfg
