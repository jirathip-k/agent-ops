from pathlib import Path

from agent_ops.config import ProjectConfig
from agent_ops.loop import run_task_loop
from agent_ops.runtimes.base import RunRequest, RunResult


class FakeRuntime:
    """Succeeds after `fail_gate_times` attempts; records prompts it received."""

    name = "fake"

    def __init__(self, tmp_path: Path, fail_gate_times: int) -> None:
        self.marker = tmp_path / "fixed"
        self.remaining_failures = fail_gate_times
        self.prompts: list[str] = []

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest) -> RunResult:
        self.prompts.append(request.prompt)
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
        else:
            self.marker.write_text("done")
        return RunResult(ok=True, text="did the thing")


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
