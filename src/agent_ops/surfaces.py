from __future__ import annotations

import json
import shlex
import string
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

from agent_ops import orca, runs
from agent_ops.utils import CommandError, run

# Orca does not index a brand-new external worktree instantly (see issue #20):
# retry the worktree card's selector a few times before giving up on it, then
# fall back once to the project root's card. Module-level so tests can
# monkeypatch `surfaces.time.sleep` without touching class internals.
_ATTACH_ATTEMPTS = 3
_ATTACH_DELAY_S = 2.0

# A background run's log is named `<label>-<YYYYMMDD>-<HHMMSS>.log`. Labels are
# derived from the issue number alone, so a fixed `<label>.log` opened `"w"`
# meant every re-dispatch and every `agent resume` silently truncated the
# previous attempt's only record (issue #92). Local time, most-significant
# first: a plain `ls` puts one issue's attempts in the order they happened, so
# telling which log is which attempt never requires opening one.
_LOG_STAMP_FORMAT = "%Y%m%d-%H%M%S"
# Two spawns of the same label within one second collide on the stamp — the
# later ones take an `a`, `b`, ... suffix instead of overwriting the earlier.
# A letter rather than `-2`: `-` sorts before `.`, so a numbered suffix would
# put the second attempt *ahead* of the first in `ls`, which is the one thing
# this naming scheme is supposed to make reliable.
_LOG_STAMP_SUFFIXES = ("", *string.ascii_lowercase)


@dataclass(frozen=True)
class Spawned:
    """Where a spawned run went, and how to address it once it is running.

    `where` is the human-readable string every caller has always printed. The
    rest is the same fact machine-readably: a dispatched run's identity used
    to be formatted into that prose and thrown away, which left a supervisor
    with nothing to name the run by (issue #98).

    Every identity field is optional and surface-specific — `handle` for an
    Orca terminal, `pid`/`log_path` for a detached background process — so a
    surface with no IDE behind it satisfies the protocol by filling in what it
    has. Nothing here is Orca-shaped: consumers treat a missing `handle` as
    "no push channel for this run" and fall back to polling, which is the
    permanent state of the background surface and of the CI lane.
    """

    where: str
    surface: str
    handle: str | None = None
    pid: int | None = None
    log_path: Path | None = None


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
    ) -> Spawned:
        """Start the command from `cwd`; return where it went and how to address it.

        `attach_path` is where the run should be *shown* (e.g. the task's
        worktree card in an IDE); it defaults to `cwd`. Surfaces without a UI
        may ignore it, but must keep run artifacts (logs) under `cwd` — the
        attach target can be a worktree that is deleted when the run succeeds.
        """
        ...


def _attempt_orca_attach(path: Path, label: str, command: list[str]) -> tuple[str | None, str]:
    """Try one `orca terminal create --worktree path:<path>`.

    Returns `(handle, stderr)`. `handle` is `None` for any failure Orca reports
    as transient (`orca.is_transient`): the card isn't indexed yet (issue #20),
    or the create timed out waiting for its handle (issue #117). Anything else
    — a bad flag, a permission refusal, Orca not running — raises `CommandError`
    immediately, since retrying it would only fail more slowly.
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
        if orca.is_transient(stderr):
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


def _attach_with_retry(path: Path, label: str, command: list[str]) -> tuple[str | None, str, bool]:
    """Retry `_attempt_orca_attach` on `path`, adopting whatever it leaves behind.

    Returns `(handle, last_error, adopted)`.

    The retry is deliberately *not* additive. Issue #117's failure message —
    "Timed out waiting for terminal handle after creation" — says the terminal
    was created and only the handle was late, so retrying blind makes a second
    one: verified in the wild as two live agent sessions on one worktree and
    one branch, with the spawn record naming only one of them. So each failed
    attempt first asks whether a terminal appeared that wasn't there before,
    and takes that handle instead of creating another.

    The baseline is a set of handles, not a count or a title, because Orca
    hands the title over to the agent the moment it starts printing — the
    orphan in #117 was already called "Bound runtime adapters with timeouts"
    by the time anyone looked. A `None` baseline means Orca could not be asked
    (typically the card is not indexed yet, so nothing can exist on it): no
    diff is possible, so nothing is adopted, and it is re-asked each round
    until it answers.
    """
    known = orca.terminal_handles(path)
    last_err = ""
    for attempt in range(_ATTACH_ATTEMPTS):
        if known is None:
            known = orca.terminal_handles(path)
        handle, stderr = _attempt_orca_attach(path, label, command)
        if handle is not None:
            return handle, "", False
        last_err = stderr
        if known is not None:
            appeared = sorted((orca.terminal_handles(path) or set()) - known)
            if appeared:
                return appeared[0], stderr, True
        if attempt < _ATTACH_ATTEMPTS - 1:
            time.sleep(_ATTACH_DELAY_S)
    return None, last_err, False


class OrcaSurface:
    """New terminal in the Orca IDE, attached to a worktree card.

    The agent process lives in an Orca-managed terminal, so the app shows it
    working live and the run survives this session ending. The terminal is
    attached to `attach_path`'s card (the task worktree); Orca can take a
    moment to index a brand-new external worktree, and `terminal create` can
    time out waiting for a handle it has already minted, so both are retried
    with backoff — adopting an already-created terminal rather than adding one
    — before falling back to the project root's card.
    """

    name = "orca"

    def available(self) -> bool:
        return orca.available()

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> Spawned:
        target = attach_path or cwd
        handle, last_err, adopted = _attach_with_retry(target, label, command)
        if handle is not None:
            where = f"orca terminal {label!r} (handle {handle})"
            if adopted:
                where += (
                    " — adopted the terminal a timed-out `orca terminal create` had already "
                    "made, rather than starting a second session on this worktree"
                )
            return Spawned(where=where, surface=self.name, handle=handle)

        has_fallback = attach_path is not None and attach_path != cwd
        if has_fallback:
            # Orca has no flag to attach a terminal to one card while running
            # the shell in another directory, so the fallback pane's shell
            # sits at `cwd` (the primary branch checkout) even though the
            # card says the worktree — say so plainly rather than leaving a
            # human to discover it via `git status` (issue #68).
            fallback_label = f"{label} (shell: project root, not the worktree)"
            handle, stderr = _attempt_orca_attach(cwd, fallback_label, command)
            if handle is not None:
                return Spawned(
                    where=(
                        f"orca terminal {label!r} (handle {handle}; {target} not indexed yet by "
                        f"Orca, fell back to project root card — shell starts at {cwd}, not "
                        f"the worktree)"
                    ),
                    surface=self.name,
                    handle=handle,
                )
            last_err = stderr

        raise CommandError(
            f"orca terminal create failed for {label!r}: no terminal on the worktree card "
            f"{target} after {_ATTACH_ATTEMPTS} attempt(s)"
            + (" plus a project-root fallback attempt" if has_fallback else "")
            + f":\n{last_err}"
        )


def _open_run_log(log_dir: Path, label: str) -> tuple[Path, TextIO]:
    """Create and open this attempt's own log; never touch an existing one.

    Opened `"x"`, so the only way this returns is with a file that did not
    exist a moment ago — a run can no more truncate a sibling's log than it
    can be handed one to append to (issue #92: an ever-growing appended file
    makes run boundaries unreadable, and nothing would prune it).
    """
    stamp = time.strftime(_LOG_STAMP_FORMAT)
    for suffix in _LOG_STAMP_SUFFIXES:
        path = log_dir / f"{label}-{stamp}{suffix}.log"
        try:
            return path, path.open("x")
        except FileExistsError:
            continue
    raise CommandError(
        f"could not open a run log for {label!r} in {log_dir}: every name for {stamp} is taken "
        f"({len(_LOG_STAMP_SUFFIXES)} spawns of the same label in one second)"
    )


class BackgroundSurface:
    """Detached process logging to <cwd>/.agent-runs/<label>-<timestamp>.log.

    Works everywhere (plain terminal, Claude Code UI, CI). Watch with
    `tail -f` on the log file — `Spawned.log_path` (and the `where` prose)
    names the file this run is actually writing, and the spawn record persists
    it (`messages.record_spawn`), so no reader has to reconstruct the
    timestamp. `attach_path` is ignored: there is no UI to attach to, and the
    log must live under `cwd` (the project root) so it survives the task
    worktree being removed on success.

    Each spawn writes a new file rather than reopening a label-derived one, so
    a re-dispatch or an `agent resume` cycle leaves the previous attempt's
    record intact (issue #92). Old logs are aged out by `runs.prune_logs` on
    the same one-week clock that prunes outcome records.
    """

    name = "background"

    def available(self) -> bool:
        return True

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> Spawned:
        log_dir = cwd / ".agent-runs"
        log_dir.mkdir(exist_ok=True)
        runs.prune_logs(log_dir)
        log_path, log_file = _open_run_log(log_dir, label)
        with log_file:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        # No `handle`: there is no IDE here and so no message bus to address.
        # A run on this surface is watched by polling, exactly as before —
        # `pid` and `log_path` are its identity instead.
        return Spawned(
            where=f"background pid {proc.pid} (watch: tail -f {log_path})",
            surface=self.name,
            pid=proc.pid,
            log_path=log_path,
        )


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
