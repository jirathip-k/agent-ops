from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol


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
