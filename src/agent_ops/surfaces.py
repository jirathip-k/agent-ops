from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Protocol

from agent_ops import orca
from agent_ops.utils import run


class Surface(Protocol):
    """Somewhere a long-running agent command can be spawned and watched.

    Mirrors the Runtime protocol: `agent dispatch` depends only on this, so
    adding a new surface (tmux, VS Code terminal, ...) is one class with
    `name`, `available()`, and `spawn()` registered in SURFACES.
    """

    name: str

    def available(self) -> bool: ...

    def spawn(self, label: str, command: list[str], cwd: Path) -> str:
        """Start the command; return a human-readable 'where it went'."""
        ...


class OrcaSurface:
    """New terminal in the Orca IDE, attached to the project's worktree card.

    The agent process lives in an Orca-managed terminal, so the app shows it
    working live and the run survives this session ending.
    """

    name = "orca"

    def available(self) -> bool:
        return orca.available()

    def spawn(self, label: str, command: list[str], cwd: Path) -> str:
        created = json.loads(
            run(
                [
                    orca.executable(),
                    "terminal",
                    "create",
                    "--worktree",
                    f"path:{cwd}",
                    "--title",
                    label,
                    "--command",
                    shlex.join(command),
                    "--json",
                ]
            ).stdout
        )
        handle = created["result"]["terminal"]["handle"]
        return f"orca terminal {label!r} (handle {handle})"


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
SURFACES: list[Surface] = [OrcaSurface(), BackgroundSurface()]


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
