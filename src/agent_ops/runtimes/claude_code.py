from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agent_ops.runtimes.base import RunRequest, RunResult
from agent_ops.utils import run


class ClaudeCodeRuntime:
    """Headless Claude Code via `claude -p`.

    Uses subscription auth locally; in CI the claude-code-action provides the
    OAuth token instead of this adapter. With `stream=True` the adapter uses
    `--output-format stream-json` and prints agent activity (tool calls, text)
    live — this is what makes an Orca terminal show real progress instead of
    silence until the stage ends.
    """

    name = "claude_code"

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def run(self, request: RunRequest) -> RunResult:
        cmd = build_command(request)
        if request.stream:
            return self._run_streaming(cmd, request)
        proc = run(cmd, cwd=request.cwd, input_text=request.prompt, check=False)
        return parse_result(proc)

    def _run_streaming(self, cmd: list[str], request: RunRequest) -> RunResult:
        proc = subprocess.Popen(
            cmd,
            cwd=request.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(request.prompt)
        proc.stdin.close()

        final: dict[str, Any] | None = None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "result":
                final = event
                continue
            summary = format_event(event, cwd=request.cwd)
            if summary:
                print(summary, flush=True)

        returncode = proc.wait()
        stderr = proc.stderr.read() if proc.stderr else ""
        if final is None:
            return RunResult(ok=returncode == 0, text=stderr.strip())
        return result_from_json(final, returncode)


def build_command(request: RunRequest) -> list[str]:
    cmd = ["claude", "-p", "--permission-mode", request.permission_mode]
    if request.system_prompt:
        cmd += ["--append-system-prompt", request.system_prompt]
    if request.model:
        cmd += ["--model", request.model]
    if request.max_turns is not None:
        cmd += ["--max-turns", str(request.max_turns)]
    if request.resume_session:
        cmd += ["--resume", request.resume_session]
    if request.allowed_tools:
        cmd += ["--allowedTools", *request.allowed_tools]
    if request.stream:
        # stream-json in print mode requires --verbose
        cmd += ["--output-format", "stream-json", "--verbose"]
    else:
        cmd += ["--output-format", "json"]
    return cmd


def format_event(event: dict[str, Any], cwd: Path | None = None) -> str | None:
    """One compact line per assistant action; None for events not worth showing."""
    if event.get("type") != "assistant":
        return None
    lines: list[str] = []
    for block in event.get("message", {}).get("content", []):
        kind = block.get("type")
        if kind == "text" and block.get("text", "").strip():
            lines.append(f"  │ {_clip(block['text'])}")
        elif kind == "tool_use":
            detail = _tool_detail(block.get("input") or {}, cwd=cwd)
            lines.append(f"  │ ⚙ {block.get('name', '?')}{': ' + detail if detail else ''}")
    return "\n".join(lines) or None


def _tool_detail(tool_input: dict[str, Any], cwd: Path | None = None) -> str:
    # description first: for Bash it is the short human summary that ships with
    # every call, and it beats echoing a 160-char shell incantation at the user
    for key in ("description", "command", "file_path", "pattern", "prompt", "query"):
        value = tool_input.get(key)
        if not value:
            continue
        text = str(value)
        if cwd is not None and key in ("command", "file_path"):
            text = _strip_cwd(text, cwd)
        return _clip(text)
    return ""


def _strip_cwd(text: str, cwd: Path) -> str:
    """Drop the run cwd from absolute paths so lines show `src/App.tsx`, not the worktree."""
    prefix = str(cwd).rstrip("/") + "/"
    return text.replace(prefix, "")


def _clip(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def result_from_json(data: dict[str, Any], returncode: int) -> RunResult:
    return RunResult(
        ok=returncode == 0 and not data.get("is_error", False),
        text=str(data.get("result", "")),
        session_id=data.get("session_id"),
        cost_usd=data.get("total_cost_usd"),
        raw=data,
    )


def parse_result(proc: subprocess.CompletedProcess[str]) -> RunResult:
    try:
        data: dict[str, Any] = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        text = proc.stdout.strip() or proc.stderr.strip()
        return RunResult(ok=proc.returncode == 0, text=text)
    return result_from_json(data, proc.returncode)
