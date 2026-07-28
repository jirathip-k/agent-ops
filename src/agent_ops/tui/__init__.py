"""One screen for the whole pipeline (issue #232) — `agent tui` / bare `agent`."""

from __future__ import annotations

from pathlib import Path


def run_tui(project_root: Path) -> Path | None:
    """Runs the TUI; returns the chat handoff path if `c` wrote one and exited
    through it, else None (an ordinary `q` quit)."""
    from agent_ops.tui.app import TuiApp

    return TuiApp(project_root).run()
