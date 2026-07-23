from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class RunResult:
    ok: bool
    text: str
    session_id: str | None = None
    cost_usd: float | None = None
    raw: dict[str, Any] | None = None


class Runtime(Protocol):
    """Adapter over a coding-agent CLI. Workflows and loops depend only on this."""

    name: str

    def available(self) -> bool: ...

    def run(self, request: RunRequest) -> RunResult: ...
