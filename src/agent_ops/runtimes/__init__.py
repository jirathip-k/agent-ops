from __future__ import annotations

from agent_ops.runtimes.base import (
    FailureKind,
    RunRequest,
    RunResult,
    Runtime,
    SpawnableRuntime,
)
from agent_ops.runtimes.claude_code import ClaudeCodeRuntime
from agent_ops.runtimes.codex import CodexRuntime

_RUNTIMES: dict[str, type] = {
    ClaudeCodeRuntime.name: ClaudeCodeRuntime,
    CodexRuntime.name: CodexRuntime,
}


def get_runtime(name: str) -> Runtime:
    try:
        return _RUNTIMES[name]()
    except KeyError:
        raise ValueError(f"Unknown runtime {name!r}. Available: {', '.join(_RUNTIMES)}") from None


def get_spawnable_runtime(name: str) -> SpawnableRuntime:
    """`get_runtime`, but only for adapters that can be launched as a session.

    Both shipped adapters qualify, so this never raises today. It exists so
    that an adapter that only knows the headless path fails at spawn time with
    a sentence a human can act on, rather than as an `AttributeError` from
    inside a workflow.
    """
    runtime = get_runtime(name)
    if not isinstance(runtime, SpawnableRuntime):
        raise ValueError(f"Runtime {name!r} cannot be started as an interactive session")
    return runtime


def runtime_names() -> list[str]:
    return list(_RUNTIMES)


__all__ = [
    "FailureKind",
    "RunRequest",
    "RunResult",
    "Runtime",
    "SpawnableRuntime",
    "get_runtime",
    "get_spawnable_runtime",
    "runtime_names",
]
