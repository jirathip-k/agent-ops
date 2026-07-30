from __future__ import annotations

import contextlib
import json
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_ops.runtimes.base import (
    FailureKind,
    IdleLineReader,
    IdleTimeout,
    RunRequest,
    RunResult,
    idle_timeout_result,
    terminate_and_reap,
    wall_clock_timeout_result,
)
from agent_ops.runtimes.credentials import environment_for
from agent_ops.utils import TIMEOUT_RETURNCODE, run

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
_PROVIDER_UNAVAILABLE_MARKERS = (
    "authentication required",
    "not logged in",
    "please log in",
    "no credentials",
)

# What `claude --permission-mode` accepts, as of CLI 2.1.x. `default` is the
# legacy spelling of `manual` — still accepted, no longer advertised in
# `--help`, and what `config/defaults.yaml` gives the planner and reviewer, so
# it stays in the list. Verify with `claude --permission-mode <anything>`: the
# CLI's rejection prints the current set.
PERMISSION_MODES = (
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "manual",
    "default",
    "dontAsk",
    "plan",
)

# Project-local settings, not the shared `.claude/settings.json`: the hook is
# one spawn's wiring, not a fact about the repo, and nothing here should ever
# reach a commit.
SETTINGS_REL = Path(".claude") / "settings.local.json"

# `SessionEnd`, and *only* `SessionEnd`. `Stop` was seeded here too until issue
# #120, on the reading that a turn ends because the agent is stuck. It does not:
# a turn ends whenever the assistant finishes speaking, which on a healthy run
# is within the first minute and then repeatedly after. Combined with
# `--if-unreported`, which binds the single report slot to the first caller,
# that guaranteed the earliest and least informative report was the one that
# stuck — every spawned agent recorded `halted` about a turn after it started,
# and stayed that way while it worked.
#
# `SessionEnd` fires when the session actually ends, which is what "stopped
# without reporting" is trying to describe. What that gives up is detecting a
# worker that goes idle mid-task; that was never what `Stop` detected, and
# issue #115 removed the case it was mostly there for by giving spawned
# sessions a permission mode they do not stall under. Two cases stay uncovered
# and stay silence the supervisor resolves by polling: the process dying
# without running anything (SIGKILL, power loss), and a worker that finishes,
# says nothing, and leaves the session open — for which `runs.spawn_state`
# truthfully reports `running` rather than guessing.
STOP_HOOK_EVENTS = ("SessionEnd",)

# Seconds Claude Code gives the hook before killing it. The hook runs in the
# session's critical path, so the bound matters: the reporting command has its
# own inner timeout, and this is the backstop under it.
_HOOK_TIMEOUT_S = 30

# `.claude/` is not in most repos' .gitignore, and a linked worktree cannot
# have its own `info/exclude` (git reads only the common one). A .gitignore
# that also ignores itself keeps the seeded files out of `git status` without
# touching anything shared — otherwise the first `git add -A` sweeps them into
# the worker's own PR.
_GITIGNORE_REL = Path(".claude") / ".gitignore"
_GITIGNORE_BODY = "# agent-ops spawn wiring; never committed.\nsettings.local.json\n.gitignore\n"


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
        # No stream to measure silence against here — `run()` (subprocess.run
        # underneath) captures everything and returns only at the end — so
        # this path gets a generous wall-clock bound instead of the streaming
        # path's idle timeout.
        child_env = environment_for(self.name)
        env_kwargs: dict[str, Any] = {"env": child_env} if child_env is not None else {}
        proc = run(
            cmd,
            cwd=request.cwd,
            input_text=request.prompt,
            check=False,
            timeout=request.run_timeout_seconds,
            **env_kwargs,
        )
        if proc.returncode == TIMEOUT_RETURNCODE:
            return wall_clock_timeout_result(request.run_timeout_seconds)
        return parse_result(proc)

    def classify_failure(self, result: RunResult) -> FailureKind:
        return classify_failure(result)

    def interactive_command(
        self, prompt: str | None, *, permission_mode: str, model: str | None = None
    ) -> list[str]:
        return build_interactive_command(prompt, permission_mode=permission_mode, model=model)

    def seed_stop_hook(self, worktree: Path, command: list[str]) -> Path | None:
        return write_stop_hook(worktree, command)

    def _run_streaming(self, cmd: list[str], request: RunRequest) -> RunResult:
        child_env = environment_for(self.name)
        env_kwargs: dict[str, Any] = {"env": child_env} if child_env is not None else {}
        proc = subprocess.Popen(
            cmd,
            cwd=request.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # A new session makes this pid its own process group leader, so
            # `terminate_and_reap` can signal the whole group — including a
            # tool call (e.g. a long Bash invocation) that inherited these
            # same pipes — rather than leaving one running with them open.
            start_new_session=sys.platform != "win32",
            **env_kwargs,
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
        reader = IdleLineReader(stdout)
        # `terminate_and_reap` on the way out unconditionally — normal return,
        # an exception raised mid-loop, everything — not just the IdleTimeout
        # branch below. `start_new_session=True` above makes this pid its own
        # process-group leader, so leaving any other exit path unsignaled lets
        # a descendant that outlives the top-level `claude` process (a
        # backgrounded tool call, a stuck subagent) get reparented to pid 1
        # and keep running (issue #279). Safe to double-call: `proc.wait()`
        # inside it is a no-op once a returncode is already cached, and
        # `_signal_process` already suppresses `ProcessLookupError`. Python
        # evaluates a `return` expression before running `finally`, so on
        # normal completion the result below is fully built from output
        # already captured before this mops up any leftover descendants.
        try:
            try:
                for line in reader.lines(request.idle_timeout_seconds):
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
            except IdleTimeout:
                terminate_and_reap(proc)
                stdin_thread.join()
                stderr_thread.join()
                if final is not None:
                    # The run itself already finished — the CLI's own result
                    # event arrived — and the child merely took too long to
                    # exit after that (e.g. slow hook teardown). The exit code
                    # from a forced kill reflects our signal, not the run, so
                    # don't let a slow goodbye turn an already-successful run
                    # into a failure.
                    return result_from_json(final, 0)
                return idle_timeout_result(request.idle_timeout_seconds, stderr=stderr_chunks[0])

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
        finally:
            terminate_and_reap(proc)


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


def build_interactive_command(
    prompt: str | None, *, permission_mode: str, model: str | None = None
) -> list[str]:
    """`claude` as a terminal session, with `prompt` as its opening brief.

    Deliberately not `build_command`'s `-p`: this is the form a human can
    watch, interrupt and type into, which is the whole point of putting a
    delegated worker on a visible surface rather than in a log file.

    `--permission-mode` is passed here for the same reason `build_command`
    passes it, and matters more: an interactive session with no mode falls back
    to asking, and a delegated worker nobody is watching just stops (#115).
    """
    cmd = ["claude", "--permission-mode", _checked_mode(permission_mode)]
    if model:
        cmd += ["--model", model]
    if prompt:
        cmd.append(prompt)
    return cmd


def _checked_mode(mode: str) -> str:
    """`mode` if the CLI takes it, else ValueError naming the ones it does.

    Refusing here rather than letting `claude` refuse is the point: the CLI's
    own rejection happens inside a terminal nobody is reading, and the session
    dies before its stop hook is ever loaded — so a mistyped mode would look
    exactly like a worker that vanished. Failing at spawn time puts the error
    in front of the person who typed it.
    """
    if mode not in PERMISSION_MODES:
        raise ValueError(
            f"unknown claude permission mode {mode!r}; "
            f"expected one of: {', '.join(PERMISSION_MODES)}"
        )
    return mode


def write_stop_hook(
    worktree: Path,
    command: list[str],
    *,
    log: Callable[[str], None] = lambda _: None,
) -> Path | None:
    """Seed `command` as this worktree's session-end hook. Best-effort.

    Must run *before* the session starts: Claude Code snapshots its hooks at
    startup, so a settings file written after the CLI launches is read by
    nobody. That ordering is the reason `workflows/spawn` creates the worktree
    itself and then attaches a terminal to it, rather than having Orca create
    the worktree and the agent in one step.

    The same file also pre-approves the command as a Bash pattern, so an agent
    that wants to report a *better* outcome than the hook's generic one ("done,
    PR #123") can run it without stopping to ask for permission.

    Merges into an existing settings file rather than replacing it, and never
    adds the same hook twice — re-spawning into a reused worktree is ordinary.
    Returns the file written, or None when it could not be (unreadable JSON, an
    unwritable path): the spawn still happens, it just falls back to polling.
    """
    path = worktree / SETTINGS_REL
    settings: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log(f"warning: could not read {path}, leaving it alone: {exc}")
            return None
        if not isinstance(loaded, dict):
            log(f"warning: {path} is not a JSON object, leaving it alone")
            return None
        settings = loaded

    entry = {
        "type": "command",
        "command": shlex.join(command),
        "timeout": _HOOK_TIMEOUT_S,
    }
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        log(f"warning: {path} has a non-object `hooks` key, leaving it alone")
        return None
    for event in STOP_HOOK_EVENTS:
        matchers = hooks.setdefault(event, [])
        if not isinstance(matchers, list):
            log(f"warning: {path} has a non-list `hooks.{event}` key, leaving it alone")
            return None
        if not any(_carries(matcher, entry["command"]) for matcher in matchers):
            matchers.append({"hooks": [entry]})

    _allow(settings, _permission_pattern(command), log=log)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n")
        gitignore = worktree / _GITIGNORE_REL
        if not gitignore.exists():
            gitignore.write_text(_GITIGNORE_BODY)
    except OSError as exc:
        log(f"warning: could not seed the stop hook at {path}: {exc}")
        return None
    return path


def _carries(matcher: Any, command: str) -> bool:
    """True if this hook matcher already runs `command`."""
    if not isinstance(matcher, dict):
        return False
    entries = matcher.get("hooks")
    if not isinstance(entries, list):
        return False
    return any(isinstance(e, dict) and e.get("command") == command for e in entries)


def _permission_pattern(command: list[str]) -> str:
    """`Bash(agent report:*)` for `["agent", "report", ...]`.

    Two tokens, not the whole command line: the pre-approval is for the
    *subcommand* the worker may call with its own arguments, not for the one
    argument vector the hook happens to use.
    """
    return f"Bash({' '.join(command[:2])}:*)"


def _allow(settings: dict[str, Any], pattern: str, *, log: Callable[[str], None]) -> None:
    """Add `pattern` to `permissions.allow`, leaving anything unexpected alone."""
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        log("warning: settings has a non-object `permissions` key; skipping the allow rule")
        return
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        log("warning: settings has a non-list `permissions.allow` key; skipping the allow rule")
        return
    if pattern not in allow:
        allow.append(pattern)


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

    Provider-wide auth failures are explicit failover signals. Transient
    markers still win over model-availability ones: a rate-limited call should
    wait for the same model, never spend the ladder on a hiccup.
    """
    haystack = " ".join(
        [
            result.text,
            str((result.raw or {}).get("stderr", "")),
            str((result.raw or {}).get("error", "")),
        ]
    ).lower()
    if result.session_id is None and any(
        marker in haystack for marker in _PROVIDER_UNAVAILABLE_MARKERS
    ):
        return FailureKind.PROVIDER_UNAVAILABLE
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
