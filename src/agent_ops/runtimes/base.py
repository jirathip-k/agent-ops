from __future__ import annotations

import contextlib
import os
import queue
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, Protocol, runtime_checkable

# Seconds between SIGTERM and escalating to SIGKILL when reaping a timed-out
# child. A wedged process may be stuck in a way that ignores a polite signal
# (blocked in a syscall), so termination always has a backstop.
TERMINATE_GRACE_SECONDS = 5.0


@dataclass(frozen=True)
class RunRequest:
    """A single agent execution: one prompt, one working directory."""

    prompt: str
    cwd: Path
    system_prompt: str | None = None
    model: str | None = None
    max_turns: int | None = None
    permission_mode: str = "acceptEdits"
    resume_session: str | None = None
    stream: bool = False
    allowed_tools: tuple[str, ...] = ()
    # Models to try, in order, if `model` turns out to be unavailable. Adapters
    # ignore this — walking the ladder is the caller's job (agent_ops.fallback).
    fallback_models: tuple[str, ...] = ()
    # Streaming only: kill (and reap) the child if it produces no output for
    # this long. Never a total-duration cap — a run still emitting events
    # keeps going no matter how long it has been running.
    idle_timeout_seconds: float = 1200.0
    # Non-streaming only: a wall-clock bound. There is no stream to measure
    # silence against here — `subprocess.run` captures everything and returns
    # only at the end — so this path needs a real (if generous) ceiling.
    run_timeout_seconds: float = 3000.0


@dataclass(frozen=True)
class RunResult:
    ok: bool
    text: str
    session_id: str | None = None
    cost_usd: float | None = None
    raw: dict[str, Any] | None = None
    # The model that actually produced this result — may differ from the one
    # the role resolved to if the ladder was walked. Artifacts must report it.
    model: str | None = None


class FailureKind(StrEnum):
    """Provider-neutral reading of why a run failed.

    Each adapter translates its own CLI's dialect into these; nothing outside
    `runtimes/` may match on vendor error strings.
    """

    # The configured model cannot serve this account right now: spend limit
    # hit, model unsupported for the auth in use, model retired. Another model
    # would work, so it is worth retrying down the ladder.
    MODEL_UNAVAILABLE = "model_unavailable"
    # Rate limited or overloaded. The same model will work shortly; swapping
    # models would be a needless downgrade.
    TRANSIENT = "transient"
    # Everything else — a failing gate, a bad prompt, the agent giving up.
    # Never a reason to change models.
    AGENT_FAILURE = "agent_failure"


class Runtime(Protocol):
    """Adapter over a coding-agent CLI. Workflows and loops depend only on this."""

    name: str

    def available(self) -> bool: ...

    def run(self, request: RunRequest) -> RunResult: ...

    def classify_failure(self, result: RunResult) -> FailureKind: ...


@runtime_checkable
class SpawnableRuntime(Runtime, Protocol):
    """A runtime that can also be *launched* as a session someone watches.

    Separate from `Runtime` because it is a strictly larger promise and only
    `agent spawn` needs it: the loop drives headless `run()` calls and has no
    use for either method here. Keeping them off `Runtime` means a runtime that
    only knows how to be driven headlessly (or a test double for the loop)
    stays a valid `Runtime`.
    """

    def interactive_command(
        self, prompt: str | None, *, permission_mode: str, model: str | None = None
    ) -> list[str]:
        """Argv that starts this CLI as a *human-shaped* session, not a `-p` run.

        `run()` is the headless path the loop drives; this is the one a surface
        hands to a terminal so a person can watch it and type into it. `prompt`
        is the opening brief, if there is one.

        `permission_mode` has no default, unlike `model` — a runtime picking its
        own model is a sane fallback, a session picking its own permission
        policy is the bug in issue #115. Adapters translate it into whatever
        their CLI understands and raise `ValueError` for a mode they cannot,
        because a session that dies on an unparsable flag never starts its stop
        hook either: it is a silent worker, the one failure this whole path
        exists to prevent.
        """
        ...

    def seed_stop_hook(self, worktree: Path, command: list[str]) -> Path | None:
        """Arrange for `command` to run when a session in `worktree` stops.

        The point is a completion signal that does not depend on the agent
        remembering to send one: an agent that dies, is interrupted, or stops
        early to escalate never reaches an instruction in its prompt, and a
        silent worker is indistinguishable from a working one (issue #113).

        Returns the file that now carries the arrangement, or None when this
        runtime has no such mechanism (or seeding failed). None is not an
        error: the caller loses the push signal and falls back to polling,
        which is all it ever had.
        """
        ...


class IdleTimeout(Exception):
    """Raised internally when a streaming child produces no output in time.

    Never meant to escape an adapter's `run()` — callers catch it and turn it
    into a normal failed `RunResult` via `idle_timeout_result` so a timeout is
    a run outcome, not a crash.
    """

    def __init__(self, idle_timeout_seconds: float) -> None:
        self.idle_timeout_seconds = idle_timeout_seconds
        super().__init__(f"no output for {idle_timeout_seconds:g}s")


class IdleLineReader:
    """Iterates a blocking stdout stream with an idle timeout.

    `for line in stream` blocks forever if the child goes quiet without
    exiting — there is no way to attach a per-read timeout to a plain text
    stream. This drains `stream` on a background thread into a queue instead,
    so the consumer can wait on `queue.get(timeout=...)`: a line arriving
    resets the wait, so a run that keeps emitting events is never cut off, no
    matter how long it has been running in total.
    """

    def __init__(self, stream: IO[str]) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._drain, args=(stream,), daemon=True)
        self._thread.start()

    def _drain(self, stream: IO[str]) -> None:
        try:
            for line in stream:
                self._queue.put(line)
        finally:
            self._queue.put(None)  # sentinel: EOF

    def lines(self, idle_timeout_seconds: float) -> Iterator[str]:
        """Yield lines as they arrive; raises `IdleTimeout` if none do in time."""
        while True:
            try:
                line = self._queue.get(timeout=idle_timeout_seconds)
            except queue.Empty:
                raise IdleTimeout(idle_timeout_seconds) from None
            if line is None:
                return
            yield line


def terminate_and_reap(
    proc: subprocess.Popen[str], *, grace_seconds: float = TERMINATE_GRACE_SECONDS
) -> None:
    """Terminate `proc` and reap it — a timed-out child must never be abandoned.

    An abandoned process keeps its worktree claimed, so a re-dispatch would
    reuse a checkout a live process still owns. SIGTERM first, escalating to
    SIGKILL if it hasn't exited within `grace_seconds`: a wedged child may be
    stuck in a way that ignores the polite signal.

    Signals the whole process group, not just `proc`'s own pid, so long as the
    caller started one (see `start_new_session` on the `Popen` this receives).
    A CLI's own tool call (e.g. a long Bash invocation) is a descendant that
    inherits the same stdout/stderr pipes; signalling only the top pid would
    leave it running with those pipes still open, and a reader thread waiting
    on EOF would hang forever — the exact class of hang this function exists
    to prevent.
    """
    _signal_process(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        _signal_process(proc, signal.SIGKILL)
        proc.wait()


def _signal_process(proc: subprocess.Popen[str], sig: int) -> None:
    if sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
            return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        proc.send_signal(sig)


def idle_timeout_result(idle_timeout_seconds: float, *, stderr: str = "") -> RunResult:
    """A normal failed `RunResult` for a streaming run killed for producing no output.

    Naming the bound in `text` is what lets `classify_failure` (and a human
    reading the log) read this as "the child stalled", not a generic crash.
    """
    return RunResult(
        ok=False,
        text=f"no output for {idle_timeout_seconds:g}s; treated as stalled and terminated",
        raw={"idle_timeout_seconds": idle_timeout_seconds, "stderr": stderr},
    )


def wall_clock_timeout_result(run_timeout_seconds: float) -> RunResult:
    """A normal failed `RunResult` for a non-streaming run past its wall-clock bound."""
    return RunResult(
        ok=False,
        text=f"did not finish within {run_timeout_seconds:g}s",
        raw={"run_timeout_seconds": run_timeout_seconds},
    )
