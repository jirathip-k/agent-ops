"""One screen for the whole pipeline (issue #232) — `agent tui` / bare `agent`."""

from __future__ import annotations

from pathlib import Path

from agent_ops.config import load_project_config


def run_tui(project_root: Path) -> Path | None:
    """Runs the TUI; returns the chat handoff path if `c` wrote one and exited
    through it, else None (an ordinary `q` quit).

    The configured theme is validated here, before the app is constructed —
    an unknown theme name is a config error at startup (issue #248), not a
    silent fallback discovered only once the screen is already up.
    """
    from textual.theme import BUILTIN_THEMES

    from agent_ops.tui.app import TuiApp

    config = load_project_config(project_root)
    theme = config.tui.theme
    if theme not in BUILTIN_THEMES:
        valid = ", ".join(sorted(BUILTIN_THEMES))
        raise ValueError(f"tui.theme: {theme!r} is not a built-in theme. Valid themes: {valid}")
    return TuiApp(project_root, theme=theme, project_config=config).run()
