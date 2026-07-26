import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_ops.runtimes import claude_code as claude_mod
from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult
from agent_ops.runtimes.claude_code import (
    PERMISSION_MODES,
    ClaudeCodeRuntime,
    build_command,
    build_interactive_command,
    classify_failure,
    format_event,
    parse_result,
)


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_parses_json_result() -> None:
    stdout = (
        '{"result": "done", "session_id": "abc-123", "total_cost_usd": 0.42, "is_error": false}'
    )
    result = parse_result(_proc(stdout))
    assert result.ok
    assert result.text == "done"
    assert result.session_id == "abc-123"
    assert result.cost_usd == 0.42


def test_is_error_flag_marks_failure() -> None:
    result = parse_result(_proc('{"result": "boom", "is_error": true}'))
    assert not result.ok
    assert result.text == "boom"


def test_non_json_output_falls_back_to_plain_text() -> None:
    result = parse_result(_proc("plain text", returncode=1, stderr="something broke"))
    assert not result.ok
    assert result.text == "plain text"


def test_format_event_shows_tool_use_with_detail() -> None:
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Let me check the tests."},
                {"type": "tool_use", "name": "Bash", "input": {"command": "uv run pytest -q"}},
            ]
        },
    }
    summary = format_event(event)
    assert summary is not None
    assert "Let me check the tests." in summary
    assert "⚙ Bash: uv run pytest -q" in summary


def test_format_event_bash_prefers_description_over_command() -> None:
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {
                        "command": 'rg -n "photoKey" /Users/x/proj/src --type ts -l',
                        "description": "Search for photoKey usages",
                    },
                },
            ]
        },
    }
    summary = format_event(event, cwd=Path("/Users/x/proj"))
    assert summary is not None
    assert "⚙ Bash: Search for photoKey usages" in summary
    assert "rg -n" not in summary


def test_format_event_bash_without_description_strips_cwd_from_command() -> None:
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "cat /Users/x/proj/src/App.tsx"},
                },
            ]
        },
    }
    summary = format_event(event, cwd=Path("/Users/x/proj"))
    assert summary is not None
    assert "⚙ Bash: cat src/App.tsx" in summary


def test_format_event_relativizes_file_path_under_cwd() -> None:
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "/Users/x/proj/src/App.tsx"},
                },
            ]
        },
    }
    summary = format_event(event, cwd=Path("/Users/x/proj"))
    assert summary is not None
    assert "⚙ Read: src/App.tsx" in summary


def test_format_event_leaves_paths_outside_cwd_absolute() -> None:
    event = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "/etc/hosts"},
                },
            ]
        },
    }
    summary = format_event(event, cwd=Path("/Users/x/proj"))
    assert summary is not None
    assert "⚙ Read: /etc/hosts" in summary


def test_format_event_clips_long_text() -> None:
    event = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "x" * 500}]},
    }
    summary = format_event(event)
    assert summary is not None
    assert len(summary) < 200
    assert summary.endswith("…")


def test_format_event_ignores_non_assistant_events() -> None:
    assert format_event({"type": "system", "subtype": "init"}) is None
    assert format_event({"type": "user", "message": {"content": []}}) is None


def test_build_command_includes_allowed_tools_and_model() -> None:
    request = RunRequest(
        prompt="fix it",
        cwd=Path("."),
        model="sonnet",
        permission_mode="acceptEdits",
        allowed_tools=("Bash(uv run pytest -q)", "Bash(uv run pytest -q:*)"),
    )
    cmd = build_command(request)
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--model") : cmd.index("--model") + 2] == ["--model", "sonnet"]
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1 : idx + 3] == ["Bash(uv run pytest -q)", "Bash(uv run pytest -q:*)"]
    assert cmd[-2:] == ["--output-format", "json"]


def test_build_command_stream_uses_stream_json_verbose() -> None:
    cmd = build_command(RunRequest(prompt="x", cwd=Path("."), stream=True))
    assert cmd[-3:] == ["--output-format", "stream-json", "--verbose"]


# --- the interactive form (`agent spawn`) ----------------------------------


def test_interactive_command_carries_a_permission_mode() -> None:
    """Without it the session asks, and a delegated worker has nobody to ask (#115)."""
    cmd = build_interactive_command("fix it", permission_mode="bypassPermissions", model="sonnet")

    assert cmd[0] == "claude"
    assert "-p" not in cmd  # still the interactive form, not the headless one
    idx = cmd.index("--permission-mode")
    assert cmd[idx + 1] == "bypassPermissions"
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert cmd[-1] == "fix it"


def test_interactive_command_passes_a_tightened_mode_through() -> None:
    cmd = build_interactive_command(None, permission_mode="plan")
    assert cmd == ["claude", "--permission-mode", "plan"]


def test_interactive_command_rejects_a_mode_the_cli_would_reject() -> None:
    """`claude` would reject it too — but inside a terminal nobody is reading,
    and the session dies before its stop hook loads, so the worker just vanishes."""
    with pytest.raises(ValueError) as exc:
        build_interactive_command("x", permission_mode="acceptEdit")

    assert "acceptEdit" in str(exc.value)
    assert "acceptEdits" in str(exc.value)  # the message names what to use instead


def test_the_modes_the_headless_path_configures_are_all_spawnable() -> None:
    """`default` is the legacy spelling planner/reviewer still use in defaults.yaml."""
    assert {"default", "acceptEdits", "plan", "bypassPermissions"} <= set(PERMISSION_MODES)


# The exact text Claude Code printed when the account hit its Fable spend
# limit mid-review on 2026-07-25 (issue #41). Kept verbatim: classification is
# only as good as the strings it was actually built against.
SPEND_LIMIT_OUTPUT = (
    "You've hit your monthly spend limit. Run /usage-credits to manage your limit and "
    "keep using Fable 5 or switch models to continue this chat."
)


def test_spend_limit_prose_classifies_as_model_unavailable() -> None:
    result = parse_result(_proc(SPEND_LIMIT_OUTPUT, returncode=1))
    assert not result.ok
    assert classify_failure(result) is FailureKind.MODEL_UNAVAILABLE


def test_unsupported_model_classifies_as_model_unavailable() -> None:
    result = parse_result(_proc('{"result": "Model not found: fable-6", "is_error": true}'))
    assert classify_failure(result) is FailureKind.MODEL_UNAVAILABLE


def test_rate_limit_classifies_as_transient() -> None:
    result = parse_result(_proc('{"result": "API Error: 429 rate_limit_error", "is_error": true}'))
    assert classify_failure(result) is FailureKind.TRANSIENT


def test_overload_classifies_as_transient() -> None:
    result = parse_result(_proc('{"result": "API Error: 529 overloaded_error", "is_error": true}'))
    assert classify_failure(result) is FailureKind.TRANSIENT


def test_ordinary_agent_error_is_not_a_model_problem() -> None:
    result = parse_result(_proc('{"result": "I could not fix the failing test", "is_error": true}'))
    assert classify_failure(result) is FailureKind.AGENT_FAILURE


def test_stderr_is_searched_when_stdout_carried_no_prose() -> None:
    result = RunResult(ok=False, text="", raw={"stderr": SPEND_LIMIT_OUTPUT})
    assert classify_failure(result) is FailureKind.MODEL_UNAVAILABLE


def test_non_streaming_run_gets_the_requests_wall_clock_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent run takes tens of minutes — `utils.run`'s short default would kill
    it — but there's no stream to watch for silence, so it still needs a real
    (generous) wall-clock bound rather than none at all."""
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs)
        return _proc('{"result": "done", "is_error": false}')

    monkeypatch.setattr(claude_mod, "run", fake_run)
    request = RunRequest(prompt="p", cwd=Path("."), stream=False, run_timeout_seconds=1234.0)
    ClaudeCodeRuntime().run(request)
    assert seen["timeout"] == 1234.0


def test_non_streaming_wall_clock_timeout_is_a_normal_failed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stream to watch for silence here, so an expired `run()` timeout must
    surface as an ordinary failed RunResult, not an escaped exception."""
    from agent_ops.utils import CommandError

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise CommandError(f"`{' '.join(cmd)}` did not finish within {kwargs['timeout']:g}s")

    monkeypatch.setattr(claude_mod, "run", fake_run)
    request = RunRequest(prompt="p", cwd=Path("."), stream=False, run_timeout_seconds=42.0)

    result = ClaudeCodeRuntime().run(request)

    assert result.ok is False
    assert "42" in result.text
    assert classify_failure(result) is FailureKind.AGENT_FAILURE


# --- _run_streaming deadlock regression tests -------------------------------
#
# These spawn a real `sys.executable -c <script>` child process to exercise
# the pipe plumbing in `_run_streaming` directly (no mocking of subprocess).
# Each call is wrapped in a helper-thread timeout guard: if the fix regresses
# and the adapter deadlocks against its child again, the test fails fast
# instead of hanging the whole suite forever.

_LARGE_PROMPT = "p" * 100_000  # well past the ~64 KiB OS pipe buffer

# Writes a large filler line to stdout *before* touching stdin at all. With
# the old synchronous `proc.stdin.write(...)` this reproduces the deadlock:
# the parent blocks writing a >64 KiB prompt while the child blocks writing
# >64 KiB of stdout that nobody is reading yet.
_CHILD_STDOUT_BEFORE_STDIN = """
import sys
sys.stdout.write("f" * 200000 + "\\n")
sys.stdout.flush()
sys.stdin.read()
sys.stdout.write('{"type": "result", "result": "large-prompt-ok", "is_error": false}\\n')
sys.stdout.flush()
"""

# Drains stdin normally, then writes >64 KiB to stderr *before* emitting the
# stdout result line. With the old code stderr is only read after stdout EOF,
# so the child blocks on the stderr write and never reaches the result line.
_CHILD_CHATTY_STDERR = """
import sys
sys.stdin.read()
sys.stderr.write("e" * 262144)
sys.stderr.flush()
sys.stdout.write('{"type": "result", "result": "stderr-ok", "is_error": false}\\n')
sys.stdout.flush()
"""

# Combines both hazards in one run.
_CHILD_LARGE_PROMPT_AND_CHATTY_STDERR = """
import sys
sys.stdout.write("f" * 200000 + "\\n")
sys.stdout.flush()
sys.stdin.read()
sys.stderr.write("e" * 262144)
sys.stderr.flush()
sys.stdout.write('{"type": "result", "result": "combined-ok", "is_error": false}\\n')
sys.stdout.flush()
"""

# No result event at all, short stderr message, non-zero exit.
_CHILD_NO_RESULT_NONZERO_EXIT = """
import sys
sys.stdin.read()
sys.stderr.write("boom")
sys.exit(1)
"""

# No result event *and* >64 KiB of stderr, non-zero exit. Exercises the
# `final is None` fallback path (`RunResult(text=stderr.strip())`) together
# with the large-stderr pipe-draining behavior, which the other chatty-stderr
# tests only ever pair with a successful result event.
_CHILD_CHATTY_STDERR_NO_RESULT_NONZERO_EXIT = """
import sys
sys.stdin.read()
sys.stderr.write("e" * 262144)
sys.stderr.flush()
sys.exit(1)
"""

# Exits immediately without ever reading stdin, despite a >64 KiB prompt
# waiting to be written.
_CHILD_EARLY_EXIT_NO_STDIN_READ = """
import sys
print('{"type": "result", "result": "early-exit-ok", "is_error": false}')
"""


def _run_streaming_with_timeout(
    cmd: list[str], request: RunRequest, timeout: float = 30.0
) -> RunResult:
    runtime = ClaudeCodeRuntime()
    results: list[RunResult] = []
    errors: list[Exception] = []

    def _target() -> None:
        try:
            results.append(runtime._run_streaming(cmd, request))
        except Exception as exc:  # surfaced via re-raise in the caller below
            errors.append(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), "_run_streaming did not return within timeout (deadlock?)"
    if errors:
        raise errors[0]
    return results[0]


def test_streaming_survives_large_prompt_written_before_child_drains_stdin() -> None:
    cmd = [sys.executable, "-c", _CHILD_STDOUT_BEFORE_STDIN]
    request = RunRequest(prompt=_LARGE_PROMPT, cwd=Path("."), stream=True)
    result = _run_streaming_with_timeout(cmd, request)
    assert result.ok
    assert result.text == "large-prompt-ok"


def test_streaming_survives_chatty_stderr_mid_run() -> None:
    cmd = [sys.executable, "-c", _CHILD_CHATTY_STDERR]
    request = RunRequest(prompt="small prompt", cwd=Path("."), stream=True)
    result = _run_streaming_with_timeout(cmd, request)
    assert result.ok
    assert result.text == "stderr-ok"


def test_streaming_survives_large_prompt_and_chatty_stderr_combined() -> None:
    cmd = [sys.executable, "-c", _CHILD_LARGE_PROMPT_AND_CHATTY_STDERR]
    request = RunRequest(prompt=_LARGE_PROMPT, cwd=Path("."), stream=True)
    result = _run_streaming_with_timeout(cmd, request)
    assert result.ok
    assert result.text == "combined-ok"


def test_streaming_no_result_event_and_nonzero_exit_returns_stderr() -> None:
    cmd = [sys.executable, "-c", _CHILD_NO_RESULT_NONZERO_EXIT]
    request = RunRequest(prompt="small prompt", cwd=Path("."), stream=True)
    result = _run_streaming_with_timeout(cmd, request)
    assert not result.ok
    assert result.text == "boom"


def test_streaming_chatty_stderr_no_result_and_nonzero_exit_returns_full_stderr() -> None:
    cmd = [sys.executable, "-c", _CHILD_CHATTY_STDERR_NO_RESULT_NONZERO_EXIT]
    request = RunRequest(prompt="small prompt", cwd=Path("."), stream=True)
    result = _run_streaming_with_timeout(cmd, request)
    assert not result.ok
    assert result.text == "e" * 262144


def test_streaming_early_exit_child_does_not_leak_broken_pipe_error() -> None:
    captured: list[threading.ExceptHookArgs] = []
    original_hook = threading.excepthook
    threading.excepthook = captured.append
    try:
        cmd = [sys.executable, "-c", _CHILD_EARLY_EXIT_NO_STDIN_READ]
        request = RunRequest(prompt=_LARGE_PROMPT, cwd=Path("."), stream=True)
        result = _run_streaming_with_timeout(cmd, request)
    finally:
        threading.excepthook = original_hook
    assert result.ok
    assert result.text == "early-exit-ok"
    assert not captured, f"unexpected exception escaped a background thread: {captured}"


# --- _run_streaming idle timeout ---------------------------------------------

# Prints once, then goes silent for far longer than any idle_timeout used
# below without exiting on its own — a stalled API call or wedged tool call
# looks exactly like this: alive, but producing nothing.
_CHILD_GOES_SILENT_THEN_SLEEPS = """
import sys, time
sys.stdin.read()
sys.stdout.write('{"type": "start"}\\n')
sys.stdout.flush()
time.sleep(30)
sys.stdout.write('{"type": "result", "result": "late", "is_error": false}\\n')
sys.stdout.flush()
"""

# Emits an event every 0.1s for a total duration well past any idle_timeout
# used below. Proves the bound is genuinely about silence, not total runtime.
_CHILD_CHATTY_ACROSS_MANY_IDLE_WINDOWS = """
import sys, time
sys.stdin.read()
for _ in range(30):
    sys.stdout.write('{"type": "assistant", "message": {"content": []}}\\n')
    sys.stdout.flush()
    time.sleep(0.05)
sys.stdout.write('{"type": "result", "result": "chatty-ok", "is_error": false}\\n')
sys.stdout.flush()
"""

# Emits its terminal result event immediately, then goes silent for far longer
# than any idle_timeout used below before actually exiting — e.g. slow hook
# teardown after the CLI has already reported its outcome.
_CHILD_RESULT_THEN_SLOW_EXIT = """
import sys, time
sys.stdin.read()
sys.stdout.write('{"type": "result", "result": "done-slow-teardown", "is_error": false}\\n')
sys.stdout.flush()
time.sleep(30)
"""


def test_streaming_idle_child_is_terminated_and_reaped() -> None:
    cmd = [sys.executable, "-c", _CHILD_GOES_SILENT_THEN_SLEEPS]
    request = RunRequest(prompt="p", cwd=Path("."), stream=True, idle_timeout_seconds=0.3)

    # `_run_streaming_with_timeout` itself asserts the call returns within its
    # own (much larger) timeout — if the child were merely abandoned rather
    # than terminated and reaped, `_run_streaming` would never return and that
    # outer assertion would fail instead.
    result = _run_streaming_with_timeout(cmd, request, timeout=10.0)

    assert not result.ok
    assert "0.3" in result.text
    assert result.raw is not None
    assert result.raw["idle_timeout_seconds"] == 0.3


def test_streaming_idle_timeout_reaches_classify_failure_as_agent_failure() -> None:
    """A timeout is a run outcome, not a crash: it must classify like any
    other failure so `loop.py`'s retry/escalate logic behaves predictably."""
    cmd = [sys.executable, "-c", _CHILD_GOES_SILENT_THEN_SLEEPS]
    request = RunRequest(prompt="p", cwd=Path("."), stream=True, idle_timeout_seconds=0.3)
    result = _run_streaming_with_timeout(cmd, request, timeout=10.0)
    assert classify_failure(result) is FailureKind.AGENT_FAILURE


def test_streaming_chatty_child_is_never_killed_regardless_of_total_duration() -> None:
    """Total duration here (~1.5s) comfortably exceeds idle_timeout_seconds (0.6s)
    — only a gap between events would trigger the bound, and there is none."""
    cmd = [sys.executable, "-c", _CHILD_CHATTY_ACROSS_MANY_IDLE_WINDOWS]
    request = RunRequest(prompt="p", cwd=Path("."), stream=True, idle_timeout_seconds=0.6)
    result = _run_streaming_with_timeout(cmd, request, timeout=10.0)
    assert result.ok
    assert result.text == "chatty-ok"


def test_streaming_idle_timeout_after_result_keeps_the_already_captured_result() -> None:
    """The idle timeout can fire *after* the terminal result event (a child
    slow to exit, e.g. hook teardown). That must not discard an already-known
    outcome — the exit code from our own forced kill isn't the run's exit code."""
    cmd = [sys.executable, "-c", _CHILD_RESULT_THEN_SLOW_EXIT]
    request = RunRequest(prompt="p", cwd=Path("."), stream=True, idle_timeout_seconds=0.3)
    result = _run_streaming_with_timeout(cmd, request, timeout=10.0)
    assert result.ok
    assert result.text == "done-slow-teardown"
