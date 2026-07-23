from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from agent_ops.runtimes.base import RunRequest, RunResult
from agent_ops.utils import run


class ClaudeCodeRuntime:
    """Headless Claude Code via `claude -p --output-format json`.

    Uses subscription auth locally; in CI the claude-code-action provides the
    OAuth token instead of this adapter.
    """

    name = "claude_code"

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def run(self, request: RunRequest) -> RunResult:
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            request.permission_mode,
        ]
        if request.system_prompt:
            cmd += ["--append-system-prompt", request.system_prompt]
        if request.model:
            cmd += ["--model", request.model]
        if request.max_turns is not None:
            cmd += ["--max-turns", str(request.max_turns)]
        if request.resume_session:
            cmd += ["--resume", request.resume_session]

        proc = run(cmd, cwd=request.cwd, input_text=request.prompt, check=False)
        return parse_result(proc)


def parse_result(proc: subprocess.CompletedProcess[str]) -> RunResult:
    try:
        data: dict[str, Any] = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        text = proc.stdout.strip() or proc.stderr.strip()
        return RunResult(ok=proc.returncode == 0, text=text)

    return RunResult(
        ok=proc.returncode == 0 and not data.get("is_error", False),
        text=str(data.get("result", "")),
        session_id=data.get("session_id"),
        cost_usd=data.get("total_cost_usd"),
        raw=data,
    )
