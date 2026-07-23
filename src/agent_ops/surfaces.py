from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Protocol

from agent_ops.utils import run


class Surface(Protocol):
    """Somewhere a long-running agent command can be spawned and watched.

    Mirrors the Runtime protocol: `agent dispatch` depends only on this, so
    adding a new surface (Orca IDE, tmux, VS Code terminal, ...) is one class
    with `name`, `available()`, and `spawn()` registered in SURFACES.
    """

    name: str

    def available(self) -> bool: ...

    def spawn(self, label: str, command: list[str], cwd: Path) -> str:
        """Start the command; return a human-readable 'where it went'."""
        ...


class HerdrSurface:
    """New tab in the user's running Herdr session, via its socket API.

    The agent process lives in the pane, so Herdr's sidebar shows live
    working/blocked/done status and the run survives this session ending.
    """

    name = "herdr"

    def available(self) -> bool:
        if shutil.which("herdr") is None:
            return False
        return run(["herdr", "tab", "list"], check=False).returncode == 0

    def spawn(self, label: str, command: list[str], cwd: Path) -> str:
        created = json.loads(
            run(
                ["herdr", "tab", "create", "--cwd", str(cwd), "--label", label, "--no-focus"]
            ).stdout
        )
        pane_id = created["result"]["root_pane"]["pane_id"]
        run(["herdr", "pane", "run", pane_id, *command])
        return f"herdr tab {label!r} (pane {pane_id})"


class BackgroundSurface:
    """Detached process logging to <cwd>/.agent-runs/<label>.log.

    Works everywhere (plain terminal, Claude Code UI, CI). Watch with
    `tail -f` on the log file.
    """

    name = "background"

    def available(self) -> bool:
        return True

    def spawn(self, label: str, command: list[str], cwd: Path) -> str:
        log_dir = cwd / ".agent-runs"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / f"{label}.log"
        with log_path.open("w") as log_file:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return f"background pid {proc.pid} (watch: tail -f {log_path})"


# Detection order for --surface auto: most visible first.
SURFACES: list[Surface] = [HerdrSurface(), BackgroundSurface()]


def pick(name: str = "auto") -> Surface:
    if name == "auto":
        for surface in SURFACES:
            if surface.available():
                return surface
    for surface in SURFACES:
        if surface.name == name:
            if not surface.available():
                raise ValueError(f"Surface {name!r} is not available right now")
            return surface
    raise ValueError(
        f"Unknown surface {name!r}. Available: auto, " + ", ".join(s.name for s in SURFACES)
    )
