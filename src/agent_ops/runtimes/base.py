from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


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
