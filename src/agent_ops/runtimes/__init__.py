from __future__ import annotations

from agent_ops.runtimes.base import RunRequest, RunResult, Runtime
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


def runtime_names() -> list[str]:
    return list(_RUNTIMES)


__all__ = ["Runtime", "RunRequest", "RunResult", "get_runtime", "runtime_names"]
