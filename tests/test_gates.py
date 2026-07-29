from pathlib import Path

from agent_ops.config import ProjectConfig
from agent_ops.gates import GateResult, GateStatus, format_failures, format_missing_gate, run_gates


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
    assert results[0].status is GateStatus.PASSED
    assert not results[1].ok
    assert results[1].status is GateStatus.FAILED
    assert "bad" in results[1].output


def test_all_gates_run_even_after_failure(tmp_path: Path) -> None:
    config = _config(test="exit 1", lint="echo fine")
    results = run_gates(config, tmp_path)
    assert len(results) == 2
    assert results[1].ok


def test_missing_binary_is_classified_missing_not_failed(tmp_path: Path) -> None:
    """`sh -c '<absent tool>'` exits 127 with dash's "not found" phrasing."""
    config = _config(test="definitely-not-a-real-binary-287")
    results = run_gates(config, tmp_path)
    assert results[0].status is GateStatus.MISSING
    assert results[0].ok is False
    assert results[0].missing_binary == "definitely-not-a-real-binary-287"


def test_bash_style_command_not_found_is_also_classified_missing(tmp_path: Path) -> None:
    """bash's "command not found" phrasing (e.g. a gate that itself shells out to bash)."""
    config = _config(test="bash -c definitely-not-a-real-binary-287-bash")
    results = run_gates(config, tmp_path)
    assert results[0].status is GateStatus.MISSING
    assert results[0].missing_binary == "definitely-not-a-real-binary-287-bash"


def test_deliberate_exit_127_with_no_not_found_text_is_failed_not_missing(tmp_path: Path) -> None:
    """The explicit no-false-positive case: a script can legitimately `exit 127`."""
    config = _config(test="echo custom failure >&2; exit 127")
    results = run_gates(config, tmp_path)
    assert results[0].status is GateStatus.FAILED
    assert results[0].missing_binary is None
    assert not results[0].ok


def test_overrunning_gate_fails_that_gate_instead_of_raising(tmp_path: Path) -> None:
    config = _config(gate_timeout_seconds=0.1, test="sleep 5", lint="echo fine")
    results = run_gates(config, tmp_path)
    assert not results[0].ok
    # a timeout must keep meaning "failure", not be mistaken for a missing binary
    assert results[0].status is GateStatus.FAILED
    assert results[0].missing_binary is None
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


def test_format_missing_gate_names_binary_command_and_config_pointers(tmp_path: Path) -> None:
    config = ProjectConfig.model_validate(
        {"commands": {"setup": "npm ci", "test": "npm run check", "requires": []}}
    )
    result = GateResult("test", "npm run check", GateStatus.MISSING, "hugo: not found", "hugo")

    message = format_missing_gate(config, result, tmp_path)

    assert "hugo" in message
    assert "npm run check" in message
    assert "commands.setup" in message
    assert "commands.requires" in message
    assert str(tmp_path / ".agent" / "config.yaml") in message
