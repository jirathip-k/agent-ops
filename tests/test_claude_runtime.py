import subprocess

from agent_ops.runtimes.claude_code import format_event, parse_result


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
