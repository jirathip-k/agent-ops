from agent_ops.runtimes.base import FailureKind, RunResult
from agent_ops.runtimes.codex import classify_failure

# The exact envelope `codex exec` wrote to stderr when `agent review --runtime
# codex` handed it a Claude model name (issue #39). Kept verbatim — matching is
# only as good as the output it was actually built against.
UNSUPPORTED_MODEL_STDERR = (
    '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
    '"message":"The \'fable\' model is not supported when using Codex with a '
    'ChatGPT account."}}'
)


def _result(stdout: str = "", stderr: str = "") -> RunResult:
    return RunResult(
        ok=False,
        text=stdout.strip() or stderr.strip(),
        raw={"stdout": stdout, "stderr": stderr, "returncode": 1},
    )


def test_unsupported_model_on_stderr_classifies_as_model_unavailable() -> None:
    assert (
        classify_failure(_result(stderr=UNSUPPORTED_MODEL_STDERR)) is FailureKind.MODEL_UNAVAILABLE
    )


def test_error_envelope_is_found_even_when_stdout_won_the_text_slot() -> None:
    """`text` prefers stdout, so the error JSON is only reachable via `raw`."""
    result = _result(stdout="thinking about it...", stderr=UNSUPPORTED_MODEL_STDERR)
    assert result.text == "thinking about it..."
    assert classify_failure(result) is FailureKind.MODEL_UNAVAILABLE


def test_retired_model_classifies_as_model_unavailable() -> None:
    stderr = (
        '{"type":"error","status":404,"error":{"type":"invalid_request_error",'
        '"message":"The model `o1-preview` does not exist or you do not have access to it."}}'
    )
    assert classify_failure(_result(stderr=stderr)) is FailureKind.MODEL_UNAVAILABLE


def test_rate_limit_classifies_as_transient() -> None:
    stderr = (
        '{"type":"error","status":429,"error":{"type":"rate_limit_error",'
        '"message":"Rate limit reached for requests."}}'
    )
    assert classify_failure(_result(stderr=stderr)) is FailureKind.TRANSIENT


def test_server_error_classifies_as_transient() -> None:
    stderr = (
        '{"type":"error","status":503,"error":{"type":"server_error",'
        '"message":"The server is overloaded."}}'
    )
    assert classify_failure(_result(stderr=stderr)) is FailureKind.TRANSIENT


def test_bad_request_that_is_not_about_a_model_is_an_agent_failure() -> None:
    """A 400 alone proves nothing — swapping models would not help here."""
    stderr = (
        '{"type":"error","status":400,"error":{"type":"invalid_request_error",'
        '"message":"Your prompt exceeds the maximum length."}}'
    )
    assert classify_failure(_result(stderr=stderr)) is FailureKind.AGENT_FAILURE


def test_plain_agent_output_is_an_agent_failure() -> None:
    assert classify_failure(_result(stdout="could not fix it")) is FailureKind.AGENT_FAILURE
