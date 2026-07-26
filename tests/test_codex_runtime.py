import subprocess
from pathlib import Path

import pytest

import agent_ops.runtimes.codex as codex_mod
from agent_ops.runtimes import claude_code as claude_mod
from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult
from agent_ops.runtimes.codex import CodexRuntime, approval_flags, classify_failure

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


# --- CodexRuntime.run --------------------------------------------------------


def _stub_run(
    monkeypatch: pytest.MonkeyPatch, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str], *, cwd: Path | None = None, check: bool = True, timeout: float | None = 0.0
    ) -> subprocess.CompletedProcess[str]:
        assert check is False, "CodexRuntime.run must not raise on a non-zero exit"
        assert timeout is None, "an agent run must opt out of `run`'s short default bound"
        calls.append(cmd)
        return subprocess.CompletedProcess(
            args=cmd, returncode=returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(codex_mod, "run", fake_run)
    return calls


def test_run_builds_base_command_with_prompt_last(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch, stdout="done")
    request = RunRequest(prompt="fix the bug", cwd=Path("."))

    CodexRuntime().run(request)

    assert calls == [["codex", "exec", "--full-auto", "--skip-git-repo-check", "fix the bug"]]


def test_run_includes_model_flag_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch, stdout="done")
    request = RunRequest(prompt="fix the bug", cwd=Path("."), model="o3")

    CodexRuntime().run(request)

    cmd = calls[0]
    assert cmd[cmd.index("--model") : cmd.index("--model") + 2] == ["--model", "o3"]
    assert cmd[-1] == "fix the bug"


def test_run_omits_model_flag_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch, stdout="done")
    request = RunRequest(prompt="fix the bug", cwd=Path("."))

    CodexRuntime().run(request)

    assert "--model" not in calls[0]


def test_run_concatenates_system_prompt_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch, stdout="done")
    request = RunRequest(prompt="fix the bug", cwd=Path("."), system_prompt="you are terse")

    CodexRuntime().run(request)

    assert calls[0][-1] == "you are terse\n\n---\n\nfix the bug"


def test_run_uses_bare_prompt_when_system_prompt_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_run(monkeypatch, stdout="done")
    request = RunRequest(prompt="fix the bug", cwd=Path("."))

    CodexRuntime().run(request)

    assert calls[0][-1] == "fix the bug"


def test_run_forwards_cwd_to_run(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_cwd: list[Path | None] = []

    def fake_run(
        cmd: list[str], *, cwd: Path | None = None, check: bool = True, timeout: float | None = 0.0
    ) -> subprocess.CompletedProcess[str]:
        assert check is False, "CodexRuntime.run must not raise on a non-zero exit"
        assert timeout is None, "an agent run must opt out of `run`'s short default bound"
        captured_cwd.append(cwd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="done", stderr="")

    monkeypatch.setattr(codex_mod, "run", fake_run)
    request = RunRequest(prompt="fix the bug", cwd=Path("/tmp/work"))

    CodexRuntime().run(request)

    assert captured_cwd == [Path("/tmp/work")]


def test_run_zero_exit_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, returncode=0, stdout="done")
    result = CodexRuntime().run(RunRequest(prompt="p", cwd=Path(".")))
    assert result.ok is True


def test_run_nonzero_exit_is_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, returncode=1, stdout="", stderr="boom")
    result = CodexRuntime().run(RunRequest(prompt="p", cwd=Path(".")))
    assert result.ok is False


def test_run_text_prefers_stripped_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, returncode=0, stdout="  the answer  \n", stderr="ignored")
    result = CodexRuntime().run(RunRequest(prompt="p", cwd=Path(".")))
    assert result.text == "the answer"


def test_run_text_falls_back_to_stderr_when_stdout_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, returncode=1, stdout="   ", stderr="  the error  ")
    result = CodexRuntime().run(RunRequest(prompt="p", cwd=Path(".")))
    assert result.text == "the error"


def test_run_raw_carries_stdout_stderr_and_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_run(monkeypatch, returncode=1, stdout="out", stderr="err")
    result = CodexRuntime().run(RunRequest(prompt="p", cwd=Path(".")))
    assert result.raw == {"stdout": "out", "stderr": "err", "returncode": 1}


# --- the interactive form (`agent spawn`) ----------------------------------


def test_interactive_command_translates_the_mode_into_sandbox_and_approval() -> None:
    """Codex has no `--permission-mode`; it splits the same question in two."""
    cmd = CodexRuntime().interactive_command(
        "fix it", permission_mode="bypassPermissions", model="gpt-5-codex"
    )

    assert cmd[0] == "codex"
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    # Never asks — the point of the default: nobody is watching to answer.
    assert cmd[cmd.index("--ask-for-approval") + 1] == "never"
    assert cmd[cmd.index("--model") + 1] == "gpt-5-codex"
    assert cmd[-1] == "fix it"


def test_a_read_only_mode_maps_to_a_read_only_sandbox() -> None:
    assert approval_flags("plan") == ["--sandbox", "read-only", "--ask-for-approval", "never"]


def test_the_bypass_mode_keeps_the_sandbox() -> None:
    """`bypassPermissions` buys a session that never stops, not one with no walls."""
    assert "--dangerously-bypass-approvals-and-sandbox" not in approval_flags("bypassPermissions")


def test_every_claude_mode_has_a_codex_translation() -> None:
    """Config carries one vocabulary; a mode the platform can set must map here."""
    for mode in claude_mod.PERMISSION_MODES:
        assert approval_flags(mode)


def test_an_untranslatable_mode_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ValueError) as exc:
        approval_flags("acceptEdit")

    assert "acceptEdit" in str(exc.value)
    assert "bypassPermissions" in str(exc.value)
