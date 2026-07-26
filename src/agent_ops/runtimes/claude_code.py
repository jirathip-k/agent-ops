from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult
from agent_ops.utils import run

# Claude Code reports these conditions as prose, not as codes, so matching is
# on the phrases the CLI actually prints. Keep the fixtures in
# tests/test_claude_runtime.py in step with anything added here.
_MODEL_UNAVAILABLE_MARKERS = (
    "spend limit",  # "…hit your monthly spend limit… or switch models to continue"
    "switch models",
    "model not found",
    "unknown model",
    "not supported",
    "no longer available",
    "has been retired",
    "is retired",
    "deprecated model",
)
_TRANSIENT_MARKERS = (
    "rate limit",
    "rate_limit_error",
    "429",
    "overloaded",
    "overloaded_error",
    "529",
    "service unavailable",
    "503",
)


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
        # timeout=None keeps today's unbounded behaviour: an agent run
        # legitimately takes tens of minutes, so `run`'s short default would
        # kill it. Bounding this path is issue #108's job — it needs an *idle*
        # timeout (silence), not a wall-clock one.
        proc = run(cmd, cwd=request.cwd, input_text=request.prompt, check=False, timeout=None)
        return parse_result(proc)

    def classify_failure(self, result: RunResult) -> FailureKind:
        return classify_failure(result)

    def _run_streaming(self, cmd: list[str], request: RunRequest) -> RunResult:
        proc = subprocess.Popen(
            cmd,
            cwd=request.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        stdin = proc.stdin
        stdout = proc.stdout
        stderr_pipe = proc.stderr

        def _write_stdin() -> None:
            with contextlib.suppress(BrokenPipeError, OSError):
                stdin.write(request.prompt)
                stdin.close()

        stderr_chunks: list[str] = [""]

        def _read_stderr() -> None:
            # Unlike `_write_stdin`, a *read* here has no BrokenPipeError hazard:
            # the child holds its stderr write-end open for its whole life, so
            # there's no early-exit race to suppress. `OSError` is still guarded
            # defensively (e.g. the fd getting torn down from elsewhere) so a
            # rare, unrelated failure here can't crash this daemon thread.
            with contextlib.suppress(OSError):
                stderr_chunks[0] = stderr_pipe.read()

        stdin_thread = threading.Thread(target=_write_stdin, daemon=True)
        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stdin_thread.start()
        stderr_thread.start()

        final: dict[str, Any] | None = None
        # Non-JSON lines are how the CLI reports refusals ("You've hit your
        # monthly spend limit…"). Dropping them would hide exactly the text
        # failure classification needs, so keep them for the fallback path.
        plain: list[str] = []
        for line in stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                plain.append(line)
                print(line, flush=True)
                continue
            if event.get("type") == "result":
                final = event
                continue
            summary = format_event(event, cwd=request.cwd)
            if summary:
                print(summary, flush=True)

        returncode = proc.wait()
        stdin_thread.join()
        stderr_thread.join()
        stderr = stderr_chunks[0]
        if final is None:
            return RunResult(
                ok=returncode == 0,
                text="\n".join(plain).strip() or stderr.strip(),
                raw={"stderr": stderr, "returncode": returncode},
            )
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


def classify_failure(result: RunResult) -> FailureKind:
    """Read Claude Code's prose error into a provider-neutral failure kind.

    Transient markers win over model-availability ones: a rate-limited call
    should wait for the same model, never spend the ladder on a hiccup.
    """
    haystack = " ".join(
        [
            result.text,
            str((result.raw or {}).get("stderr", "")),
            str((result.raw or {}).get("error", "")),
        ]
    ).lower()
    if any(marker in haystack for marker in _TRANSIENT_MARKERS):
        return FailureKind.TRANSIENT
    if any(marker in haystack for marker in _MODEL_UNAVAILABLE_MARKERS):
        return FailureKind.MODEL_UNAVAILABLE
    return FailureKind.AGENT_FAILURE


def parse_result(proc: subprocess.CompletedProcess[str]) -> RunResult:
    try:
        data: dict[str, Any] = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        text = proc.stdout.strip() or proc.stderr.strip()
        return RunResult(ok=proc.returncode == 0, text=text)
    return result_from_json(data, proc.returncode)
