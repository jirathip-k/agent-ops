import subprocess
from pathlib import Path

from agent_ops.runtimes.base import RunRequest
from agent_ops.runtimes.claude_code import build_command, format_event, parse_result


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
