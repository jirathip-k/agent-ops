from pathlib import Path

from agent_ops.config import ProjectConfig
from agent_ops.fallback import artifact_footer, model_note, pin_to_model, run_with_fallback
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

    assert model_note(request, RunResult(ok=True, text="", model="fable")) == "model: fable"
    substituted = model_note(request, RunResult(ok=True, text="", model="opus"))
    assert "opus" in substituted and "FALLBACK" in substituted and "fable" in substituted
    assert "model: opus" in artifact_footer(request, RunResult(ok=True, text="", model="opus"))


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
