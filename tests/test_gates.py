from pathlib import Path

from agent_ops.config import ProjectConfig
from agent_ops.gates import format_failures, run_gates


def _config(gate_timeout_seconds: float = 1800.0, **commands: str) -> ProjectConfig:
    return ProjectConfig.model_validate(
        {
            "commands": commands,
            "loop": {
                "gates": ["test", "lint", "typecheck"],
                "gate_timeout_seconds": gate_timeout_seconds,
            },
        }
    )


def test_passing_and_failing_gates(tmp_path: Path) -> None:
    config = _config(test="echo ok", lint="echo bad >&2; exit 1")
    results = run_gates(config, tmp_path)
    assert [r.name for r in results] == ["test", "lint"]  # typecheck unset → skipped
    assert results[0].ok
    assert not results[1].ok
    assert "bad" in results[1].output


def test_all_gates_run_even_after_failure(tmp_path: Path) -> None:
    config = _config(test="exit 1", lint="echo fine")
    results = run_gates(config, tmp_path)
    assert len(results) == 2
    assert results[1].ok


def test_overrunning_gate_fails_that_gate_instead_of_raising(tmp_path: Path) -> None:
    config = _config(gate_timeout_seconds=0.1, test="sleep 5", lint="echo fine")
    results = run_gates(config, tmp_path)
    assert not results[0].ok
    assert "sh -c sleep 5" in results[0].output
    assert "0.1s" in results[0].output
    # the run continues: a wedged gate is a gate failure, not a crash
    assert results[1].ok


def test_format_failures_names_gate_and_command(tmp_path: Path) -> None:
    config = _config(test="exit 1")
    failures = [r for r in run_gates(config, tmp_path) if not r.ok]
    text = format_failures(failures)
    assert "`test`" in text
    assert "exit 1" in text
