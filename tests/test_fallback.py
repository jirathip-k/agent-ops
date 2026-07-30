from pathlib import Path

from agent_ops.config import ProjectConfig
from agent_ops.fallback import (
    ProviderRuntime,
    RuntimeChain,
    artifact_footer,
    model_note,
    pin_to_model,
    run_with_fallback,
)
from agent_ops.loop import run_task_loop
from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult


class ScriptedRuntime:
    """Fails for every model in `unavailable`; records the models it was asked for."""

    name = "scripted"

    def __init__(
        self,
        unavailable: set[str] | None = None,
        kind: FailureKind = FailureKind.MODEL_UNAVAILABLE,
    ) -> None:
        self.unavailable = unavailable or set()
        self.kind = kind
        self.models: list[str | None] = []

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest) -> RunResult:
        self.models.append(request.model)
        if request.model in self.unavailable:
            return RunResult(ok=False, text=f"{request.model} is unavailable")
        return RunResult(ok=True, text=f"answered by {request.model}")

    def classify_failure(self, result: RunResult) -> FailureKind:
        return self.kind


def _request(tmp_path: Path, **kwargs: object) -> RunRequest:
    return RunRequest(prompt="do the thing", cwd=tmp_path, **kwargs)  # type: ignore[arg-type]


def test_no_ladder_means_exactly_one_invocation(tmp_path: Path) -> None:
    """The untouched path: a run that never hits a limit behaves as before."""
    runtime = ScriptedRuntime()
    result = run_with_fallback(runtime, _request(tmp_path, model="fable"))

    assert result.ok
    assert runtime.models == ["fable"]
    assert result.model == "fable"


def test_fallback_taken_when_model_is_unavailable(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(unavailable={"fable"})
    events: list[str] = []

    result = run_with_fallback(
        runtime,
        _request(tmp_path, model="fable", fallback_models=("opus", "sonnet")),
        on_event=events.append,
    )

    assert result.ok
    assert result.model == "opus"
    assert runtime.models == ["fable", "opus"]
    # the substitution is loud, and names both models
    assert any("MODEL FALLBACK" in e and "'fable'" in e and "'opus'" in e for e in events)


def test_fallback_walks_multiple_rungs(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(unavailable={"fable", "opus"})
    result = run_with_fallback(
        runtime, _request(tmp_path, model="fable", fallback_models=("opus", "sonnet"))
    )

    assert result.ok
    assert result.model == "sonnet"
    assert runtime.models == ["fable", "opus", "sonnet"]


def test_fallback_exhausted_returns_the_last_failure(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(unavailable={"fable", "opus"})
    events: list[str] = []

    result = run_with_fallback(
        runtime,
        _request(tmp_path, model="fable", fallback_models=("opus",)),
        on_event=events.append,
    )

    assert not result.ok
    assert result.model == "opus"
    assert runtime.models == ["fable", "opus"]
    assert any("exhausted" in e for e in events)


def test_agent_failure_does_not_swap_models(tmp_path: Path) -> None:
    """The agent's own error must never be answered with a different model."""
    runtime = ScriptedRuntime(unavailable={"fable"}, kind=FailureKind.AGENT_FAILURE)

    result = run_with_fallback(
        runtime, _request(tmp_path, model="fable", fallback_models=("opus",))
    )

    assert not result.ok
    assert runtime.models == ["fable"]
    assert result.model == "fable"


def test_transient_failure_does_not_swap_models(tmp_path: Path) -> None:
    """429/529 means wait for the same model — the CLIs already back off."""
    runtime = ScriptedRuntime(unavailable={"fable"}, kind=FailureKind.TRANSIENT)

    result = run_with_fallback(
        runtime, _request(tmp_path, model="fable", fallback_models=("opus",))
    )

    assert not result.ok
    assert runtime.models == ["fable"]


def test_pin_to_model_keeps_only_the_rungs_below(tmp_path: Path) -> None:
    request = _request(tmp_path, model="fable", fallback_models=("opus", "sonnet"))

    pinned = pin_to_model(request, "opus")

    assert pinned.model == "opus"
    assert pinned.fallback_models == ("sonnet",)


def test_pin_to_model_ignores_a_model_outside_the_ladder(tmp_path: Path) -> None:
    request = _request(tmp_path, model="fable", fallback_models=("opus",))
    assert pin_to_model(request, "haiku") is request


def test_model_note_flags_a_substitution(tmp_path: Path) -> None:
    request = _request(tmp_path, model="fable")

    configured = RunResult(
        ok=True,
        text="",
        model="fable",
        provider="claude_code",
        configured_provider="claude_code",
        configured_model="fable",
    )
    assert model_note(request, configured) == "provider: claude_code, model: fable"
    substituted_result = RunResult(
        ok=True,
        text="",
        model="opus",
        provider="claude_code",
        configured_provider="claude_code",
        configured_model="fable",
    )
    substituted = model_note(request, substituted_result)
    assert "opus" in substituted and "FALLBACK" in substituted and "fable" in substituted
    footer = artifact_footer(request, substituted_result)
    assert "provider: claude_code, model: opus" in footer


class ProviderFake:
    """Executable fake adapter with provider-specific models and classifications."""

    def __init__(
        self,
        name: str,
        *,
        unavailable_models: set[str] | None = None,
        failure_kind: FailureKind = FailureKind.MODEL_UNAVAILABLE,
        installed: bool = True,
        success_text: str = "done",
    ) -> None:
        self.name = name
        self.unavailable_models = unavailable_models or set()
        self.failure_kind = failure_kind
        self.installed = installed
        self.success_text = success_text
        self.models: list[str | None] = []

    def available(self) -> bool:
        return self.installed

    def run(self, request: RunRequest) -> RunResult:
        self.models.append(request.model)
        if request.model in self.unavailable_models:
            return RunResult(ok=False, text=f"{self.name}/{request.model} refused")
        return RunResult(ok=True, text=self.success_text)

    def classify_failure(self, result: RunResult) -> FailureKind:
        return self.failure_kind


def _chain(*providers: ProviderRuntime) -> RuntimeChain:
    return RuntimeChain(list(providers))


def test_provider_fallback_exhausts_primary_model_ladder_then_uses_own_tier(
    tmp_path: Path,
) -> None:
    claude = ProviderFake("claude_code", unavailable_models={"fable", "opus"})
    codex = ProviderFake("codex")
    chain = _chain(
        ProviderRuntime(claude, "fable", ("opus",)),
        ProviderRuntime(codex, "gpt-smart", ("gpt-small",)),
    )
    events: list[str] = []

    result = run_with_fallback(chain, _request(tmp_path, model="fable"), events.append)

    assert result.ok
    assert (result.provider, result.model) == ("codex", "gpt-smart")
    assert (result.configured_provider, result.configured_model) == ("claude_code", "fable")
    assert claude.models == ["fable", "opus"]
    assert codex.models == ["gpt-smart"]
    assert any("PROVIDER FALLBACK" in event and "claude_code" in event for event in events)
    note = model_note(_request(tmp_path, model="fable"), result)
    assert "provider: codex, model: gpt-smart" in note and "claude_code / fable" in note


def test_explicit_provider_unavailability_skips_its_remaining_models(tmp_path: Path) -> None:
    claude = ProviderFake(
        "claude_code",
        unavailable_models={"fable"},
        failure_kind=FailureKind.PROVIDER_UNAVAILABLE,
    )
    codex = ProviderFake("codex")
    chain = _chain(
        ProviderRuntime(claude, "fable", ("opus",)),
        ProviderRuntime(codex, "gpt-smart"),
    )

    result = run_with_fallback(chain, _request(tmp_path, model="fable"))

    assert result.ok and result.provider == "codex"
    assert claude.models == ["fable"]
    assert codex.models == ["gpt-smart"]


def test_missing_primary_cli_is_an_explicit_provider_fallback(tmp_path: Path) -> None:
    claude = ProviderFake("claude_code", installed=False)
    codex = ProviderFake("codex")
    events: list[str] = []
    chain = _chain(
        ProviderRuntime(claude, "fable"),
        ProviderRuntime(codex, "gpt-smart"),
    )

    result = run_with_fallback(chain, _request(tmp_path, model="fable"), events.append)

    assert result.ok and result.provider == "codex"
    assert claude.models == []
    assert codex.models == ["gpt-smart"]
    assert any("is unavailable" in event and "codex" in event for event in events)


def test_agent_failure_never_switches_provider(tmp_path: Path) -> None:
    claude = ProviderFake(
        "claude_code",
        unavailable_models={"fable"},
        failure_kind=FailureKind.AGENT_FAILURE,
    )
    codex = ProviderFake("codex")
    chain = _chain(
        ProviderRuntime(claude, "fable", ("opus",)),
        ProviderRuntime(codex, "gpt-smart"),
    )
    config = ProjectConfig.model_validate({"loop": {"max_attempts": 3, "gates": []}})

    outcome = run_task_loop(chain, _request(tmp_path, model="fable"), config, tmp_path)

    assert not outcome.ok
    assert claude.models == ["fable", "fable", "fable"]
    assert codex.models == []


def test_transient_throttling_never_switches_provider(tmp_path: Path) -> None:
    claude = ProviderFake(
        "claude_code",
        unavailable_models={"fable"},
        failure_kind=FailureKind.TRANSIENT,
    )
    codex = ProviderFake("codex")
    chain = _chain(
        ProviderRuntime(claude, "fable"),
        ProviderRuntime(codex, "gpt-smart"),
    )
    config = ProjectConfig.model_validate({"loop": {"max_attempts": 3, "gates": []}})

    outcome = run_task_loop(chain, _request(tmp_path, model="fable"), config, tmp_path)

    assert not outcome.ok
    assert claude.models == ["fable", "fable", "fable"]
    assert codex.models == []


def test_rejected_output_never_switches_provider(tmp_path: Path) -> None:
    claude = ProviderFake("claude_code", success_text="VERDICT: REQUEST CHANGES")
    codex = ProviderFake("codex")
    chain = _chain(
        ProviderRuntime(claude, "fable"),
        ProviderRuntime(codex, "gpt-smart"),
    )

    result = run_with_fallback(chain, _request(tmp_path, model="fable"))

    assert result.ok and "REQUEST CHANGES" in result.text
    assert claude.models == ["fable"]
    assert codex.models == []


def test_gate_feedback_retries_pin_provider_and_model(tmp_path: Path) -> None:
    claude = ProviderFake("claude_code", unavailable_models={"fable"})
    codex = ProviderFake("codex", unavailable_models={"gpt-smart"})
    chain = _chain(
        ProviderRuntime(claude, "fable"),
        ProviderRuntime(codex, "gpt-smart", ("gpt-fast",)),
    )
    config = ProjectConfig.model_validate(
        {
            "commands": {"test": f"test -f {tmp_path / 'never'}"},
            "loop": {"max_attempts": 3, "gates": ["test"]},
        }
    )

    outcome = run_task_loop(chain, _request(tmp_path, model="fable"), config, tmp_path)

    assert not outcome.ok
    assert claude.models == ["fable"]
    assert codex.models == ["gpt-smart", "gpt-fast", "gpt-fast", "gpt-fast"]
    assert outcome.last_result is not None
    assert (outcome.last_result.configured_provider, outcome.last_result.configured_model) == (
        "claude_code",
        "fable",
    )


class LimitedThenGateFailingRuntime:
    """First model is spend-limited; the replacement runs but never fixes the gate."""

    name = "limited"

    def __init__(self, unavailable: str) -> None:
        self.unavailable = unavailable
        self.models: list[str | None] = []

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest) -> RunResult:
        self.models.append(request.model)
        if request.model == self.unavailable:
            return RunResult(ok=False, text="You've hit your monthly spend limit.")
        return RunResult(ok=True, text="tried my best")

    def classify_failure(self, result: RunResult) -> FailureKind:
        if "spend limit" in result.text:
            return FailureKind.MODEL_UNAVAILABLE
        return FailureKind.AGENT_FAILURE


def test_loop_keeps_the_substitute_across_retries(tmp_path: Path) -> None:
    """Once fable has refused, later attempts must not walk back up into it."""
    runtime = LimitedThenGateFailingRuntime(unavailable="fable")
    config = ProjectConfig.model_validate(
        {
            "commands": {"test": f"test -f {tmp_path / 'never'}"},
            "loop": {"max_attempts": 3, "gates": ["test"]},
        }
    )
    request = RunRequest(prompt="fix", cwd=tmp_path, model="fable", fallback_models=("opus",))

    outcome = run_task_loop(runtime, request, config, tmp_path)

    assert not outcome.ok
    # fable is tried once, then every remaining attempt goes straight to opus
    assert runtime.models == ["fable", "opus", "opus", "opus"]


def test_loop_stops_when_the_ladder_is_exhausted(tmp_path: Path) -> None:
    """No model left is not a retryable condition — do not burn the attempts."""
    runtime = LimitedThenGateFailingRuntime(unavailable="fable")
    config = ProjectConfig.model_validate({"loop": {"max_attempts": 3, "gates": []}})
    request = RunRequest(prompt="fix", cwd=tmp_path, model="fable")
    events: list[str] = []

    outcome = run_task_loop(runtime, request, config, tmp_path, on_event=events.append)

    assert not outcome.ok
    assert outcome.attempts == 1
    assert runtime.models == ["fable"]
    assert any("no fallback model left" in e for e in events)
