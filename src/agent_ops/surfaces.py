from __future__ import annotations

import json
import shlex
import subprocess
import time
from pathlib import Path
from typing import Protocol

from agent_ops import orca
from agent_ops.utils import CommandError, run

# Orca does not index a brand-new external worktree instantly (see issue #20):
# retry the worktree card's selector a few times before giving up on it, then
# fall back once to the project root's card. Module-level so tests can
# monkeypatch `surfaces.time.sleep` without touching class internals.
_ATTACH_ATTEMPTS = 3
_ATTACH_DELAY_S = 2.0
_SELECTOR_NOT_FOUND = "selector_not_found"


class Surface(Protocol):
    """Somewhere a long-running agent command can be spawned and watched.

    Mirrors the Runtime protocol: `agent dispatch` depends only on this, so
    adding a new surface (tmux, VS Code terminal, ...) is one class with
    `name`, `available()`, and `spawn()` registered in SURFACES.
    """

    name: str

    def available(self) -> bool: ...

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> str:
        """Start the command from `cwd`; return a human-readable 'where it went'.

        `attach_path` is where the run should be *shown* (e.g. the task's
        worktree card in an IDE); it defaults to `cwd`. Surfaces without a UI
        may ignore it, but must keep run artifacts (logs) under `cwd` — the
        attach target can be a worktree that is deleted when the run succeeds.
        """
        ...


def _attempt_orca_attach(path: Path, label: str, command: list[str]) -> tuple[str | None, str]:
    """Try one `orca terminal create --worktree path:<path>`.

    Returns `(handle, stderr)`. `handle` is `None` when Orca reports
    `selector_not_found` (the card isn't indexed yet — worth retrying).
    Any other failure (bad flags, orca crash, ...) raises `CommandError`
    immediately since retrying it would not help.
    """
    proc = run(
        [
            orca.executable(),
            "terminal",
            "create",
            "--worktree",
            f"path:{path}",
            "--title",
            label,
            "--command",
            shlex.join(command),
            "--json",
        ],
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        if _SELECTOR_NOT_FOUND in stderr:
            return None, stderr
        raise CommandError(f"`orca terminal create` failed:\n{stderr}")
    try:
        payload = json.loads(proc.stdout)
        handle = payload["result"]["terminal"]["handle"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CommandError(
            f"`orca terminal create` returned an unexpected response: {proc.stdout.strip()}"
        ) from exc
    return handle, ""


class OrcaSurface:
    """New terminal in the Orca IDE, attached to a worktree card.

    The agent process lives in an Orca-managed terminal, so the app shows it
    working live and the run survives this session ending. The terminal is
    attached to `attach_path`'s card (the task worktree); Orca can take a
    moment to index a brand-new external worktree, so a `selector_not_found`
    response is retried with backoff before falling back to the project
    root's card.
    """

    name = "orca"

    def available(self) -> bool:
        return orca.available()

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> str:
        target = attach_path or cwd
        last_err = ""
        for attempt in range(_ATTACH_ATTEMPTS):
            handle, stderr = _attempt_orca_attach(target, label, command)
            if handle is not None:
                return f"orca terminal {label!r} (handle {handle})"
            last_err = stderr
            if attempt < _ATTACH_ATTEMPTS - 1:
                time.sleep(_ATTACH_DELAY_S)

        has_fallback = attach_path is not None and attach_path != cwd
        if has_fallback:
            handle, stderr = _attempt_orca_attach(cwd, label, command)
            if handle is not None:
                return (
                    f"orca terminal {label!r} (handle {handle}; {target} not indexed "
                    f"yet by Orca, fell back to project root card)"
                )
            last_err = stderr

        raise CommandError(
            f"orca terminal create failed for {label!r}: worktree card {target} was never "
            f"indexed by Orca after {_ATTACH_ATTEMPTS} attempt(s)"
            + (" plus a project-root fallback attempt" if has_fallback else "")
            + f":\n{last_err}"
        )


class BackgroundSurface:
    """Detached process logging to <cwd>/.agent-runs/<label>.log.

    Works everywhere (plain terminal, Claude Code UI, CI). Watch with
    `tail -f` on the log file. `attach_path` is ignored: there is no UI to
    attach to, and the log must live under `cwd` (the project root) so it
    survives the task worktree being removed on success.
    """

    name = "background"

    def available(self) -> bool:
        return True

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> str:
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
