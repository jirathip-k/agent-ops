"""One screen for the whole pipeline (issue #232) — `agent tui` / bare `agent`."""

from __future__ import annotations

from pathlib import Path

from agent_ops.config import load_project_config


def run_tui(project_root: Path) -> tuple[Path | None, str | None]:
    """Runs the TUI; returns `(path, chat_sink_error)`.

    `path` is the chat handoff path if `c` wrote one and exited through it,
    else None (an ordinary `q` quit). `chat_sink_error` is non-None only when
    a configured `tui.chat_sink` failed at send time and the handoff fell
    back to the file sink (issue #252) — always None otherwise, including
    when auto-probing (no `tui.chat_sink` configured) exhausts every
    candidate and lands on the file sink on its own.

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
    app = TuiApp(project_root, theme=theme, project_config=config)
    path = app.run()
    return path, app.chat_sink_error
