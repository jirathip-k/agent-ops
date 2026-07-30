from pathlib import Path

from agent_ops.config import ProjectConfig
from agent_ops.loop import run_task_loop
from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult


class FakeRuntime:
    """Succeeds after `fail_gate_times` attempts; records prompts it received."""

    name = "fake"

    def __init__(self, tmp_path: Path, fail_gate_times: int) -> None:
        self.marker = tmp_path / "fixed"
        self.remaining_failures = fail_gate_times
        self.prompts: list[str] = []
        self.models: list[str | None] = []

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest) -> RunResult:
        self.prompts.append(request.prompt)
        self.models.append(request.model)
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
        else:
            self.marker.write_text("done")
        return RunResult(ok=True, text="did the thing")

    def classify_failure(self, result: RunResult) -> FailureKind:
        return FailureKind.AGENT_FAILURE


def _config(marker: Path, max_attempts: int) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "commands": {"test": f"test -f {marker}"},
            "loop": {"max_attempts": max_attempts, "gates": ["test"]},
        }
    )


def test_loop_retries_until_gates_pass(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path, fail_gate_times=2)
    config = _config(runtime.marker, max_attempts=3)
    request = RunRequest(prompt="fix the bug", cwd=tmp_path)

    outcome = run_task_loop(runtime, request, config, tmp_path)

    assert outcome.ok
    assert outcome.attempts == 3
    assert outcome.missing_gate is None
    assert len(outcome.gate_results) == 1
    assert outcome.gate_results[0].name == "test"
    assert outcome.gate_results[0].command == f"test -f {runtime.marker}"
    assert outcome.gate_results[0].ok is True
    # retry prompts carry the original task plus the gate failure report
    assert "fix the bug" in runtime.prompts[1]
    assert "Verification failures" in runtime.prompts[1]
    assert "FAILED" in runtime.prompts[1]


def test_loop_gives_up_after_max_attempts(tmp_path: Path) -> None:
    runtime = FakeRuntime(tmp_path, fail_gate_times=99)
    config = _config(runtime.marker, max_attempts=2)

    outcome = run_task_loop(runtime, RunRequest(prompt="fix", cwd=tmp_path), config, tmp_path)

    assert not outcome.ok
    assert outcome.attempts == 2
    assert outcome.gate_failures and outcome.gate_failures[0].name == "test"
    assert outcome.missing_gate is None


def test_loop_aborts_on_missing_binary_without_consuming_more_attempts(tmp_path: Path) -> None:
    """A gate whose binary isn't on PATH is an environment gap, not a code failure —
    it must not be retried against the attempt budget (issue #287)."""
    runtime = FakeRuntime(tmp_path, fail_gate_times=0)
    config = ProjectConfig.model_validate(
        {
            "commands": {"test": "definitely-not-a-real-binary-287"},
            "loop": {"max_attempts": 3, "gates": ["test"]},
        }
    )
    request = RunRequest(prompt="fix the bug", cwd=tmp_path)

    outcome = run_task_loop(runtime, request, config, tmp_path)

    assert outcome.ok is False
    assert outcome.attempts == 1
    assert outcome.missing_gate is not None
    assert outcome.missing_gate.name == "test"
    assert outcome.missing_gate.missing_binary == "definitely-not-a-real-binary-287"
    assert len(runtime.prompts) == 1  # the runtime ran exactly once, no retry


def test_loop_still_retries_normally_on_a_timeout_gate(tmp_path: Path) -> None:
    """A gate that times out keeps meaning "failure" — it must not be confused with
    `GateStatus.MISSING` and must still retry through the ordinary attempt budget."""
    runtime = FakeRuntime(tmp_path, fail_gate_times=99)
    config = ProjectConfig.model_validate(
        {
            "commands": {"test": "sleep 5"},
            "loop": {"max_attempts": 2, "gates": ["test"], "gate_timeout_seconds": 0.1},
        }
    )
    request = RunRequest(prompt="fix the bug", cwd=tmp_path)

    outcome = run_task_loop(runtime, request, config, tmp_path)

    assert outcome.ok is False
    assert outcome.attempts == 2
    assert outcome.missing_gate is None
    assert outcome.gate_failures and outcome.gate_failures[0].name == "test"
    assert len(runtime.prompts) == 2  # both attempts really ran


def test_failing_gates_never_swap_the_model(tmp_path: Path) -> None:
    """A gate failure is the agent's fault, not the model's — no substitution."""
    runtime = FakeRuntime(tmp_path, fail_gate_times=99)
    config = _config(runtime.marker, max_attempts=3)
    request = RunRequest(prompt="fix", cwd=tmp_path, model="fable", fallback_models=("opus",))

    outcome = run_task_loop(runtime, request, config, tmp_path)

    assert not outcome.ok
    # every attempt ran on the configured model; the ladder was never touched
    assert runtime.models == ["fable", "fable", "fable"]
    assert outcome.last_result is not None and outcome.last_result.model == "fable"
