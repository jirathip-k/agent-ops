"""One screen for the whole pipeline (issue #232) — `agent tui` / bare `agent`."""

from __future__ import annotations

from pathlib import Path


def run_tui(project_root: Path) -> None:
    from agent_ops.tui.app import TuiApp

    TuiApp(project_root).run()
