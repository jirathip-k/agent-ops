import subprocess

from agent_ops.runtimes.claude_code import parse_result


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
