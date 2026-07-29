from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_ops.cli import app
from agent_ops.utils import CommandError
from agent_ops.workflows import evolve

runner = CliRunner()


def _row(conclusion: str = "success", index: int = 0) -> evolve.SurveyRow:
    return evolve.SurveyRow(
        when=datetime.now(UTC),
        lane="spec",
        trigger="t",
        conclusion=conclusion,
        duration="-",
        url=f"https://x/{index}",
        source="ci",
    )


def test_evolve_unknown_lane_exits_1_and_lists_valid_lanes(tmp_path: Path) -> None:
    result = runner.invoke(app, ["evolve", "nope", "--dry-run", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "unknown lane" in result.stderr
    assert "spec" in result.stderr
    assert "triage" in result.stderr


def test_evolve_promote_lane_passes_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `promote` is a real CI lane (status.LANES) with no prompts/tasks/promote.md,
    # so it must pass lane validation even though prompts.task_names() excludes it.
    monkeypatch.setattr(evolve, "gather", lambda *args, **kwargs: ([], []))

    result = runner.invoke(app, ["evolve", "promote", "--dry-run", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "unknown lane" not in result.stderr


def test_evolve_without_dry_run_calls_run_evolve_and_reports_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_run_evolve(root: Path, lane: str, **kwargs: Any) -> list[evolve.EvolveChange]:
        seen["root"] = root
        seen["lane"] = lane
        seen["kwargs"] = kwargs
        return []

    monkeypatch.setattr(evolve, "run_evolve", fake_run_evolve)

    result = runner.invoke(app, ["evolve", "spec", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "no-op" in result.output
    assert seen["lane"] == "spec"
    assert seen["kwargs"] == {"window_days": 30, "min_runs": 5}


def test_evolve_without_dry_run_reports_opened_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changes = [evolve.EvolveChange("drift", "tightened an instruction", "https://x/runs/1")]
    monkeypatch.setattr(evolve, "run_evolve", lambda *a, **k: changes)

    result = runner.invoke(app, ["evolve", "spec", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "1 change(s)" in result.output


def test_evolve_without_dry_run_failure_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_run_evolve(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("evolve touched disallowed path(s): ['prompts/orchestrator.md']")

    monkeypatch.setattr(evolve, "run_evolve", failing_run_evolve)

    result = runner.invoke(app, ["evolve", "spec", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "disallowed path" in result.stderr


def test_evolve_dry_run_below_min_runs_still_prints_the_survey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row(index=i) for i in range(2)]
    monkeypatch.setattr(evolve, "gather", lambda *args, **kwargs: (rows, []))

    result = runner.invoke(app, ["evolve", "spec", "--dry-run", "--project", str(tmp_path)])

    assert result.exit_code == 0
    for row in rows:
        assert row.url in result.output


def test_evolve_dry_run_below_min_runs_states_the_reason_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row(index=i) for i in range(2)]
    monkeypatch.setattr(evolve, "gather", lambda *args, **kwargs: (rows, []))

    result = runner.invoke(app, ["evolve", "spec", "--dry-run", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "2 run(s)" in result.output
    assert "30d" in result.output
    assert "baseline for spec" not in result.output  # only the reason is printed, no baseline block


def test_evolve_dry_run_at_or_above_min_runs_prints_survey_and_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row("success" if i % 2 else "failure", index=i) for i in range(6)]
    monkeypatch.setattr(evolve, "gather", lambda *args, **kwargs: (rows, []))

    result = runner.invoke(app, ["evolve", "spec", "--dry-run", "--project", str(tmp_path)])

    assert result.exit_code == 0
    for row in rows:
        assert row.url in result.output
    assert "baseline for spec" in result.output
    assert "6 run(s)" in result.output


def test_evolve_dry_run_passes_window_and_min_runs_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, int] = {}

    def fake_gather(
        root: Path, lane: str, *, now: datetime, window_days: int, limit: int = 100
    ) -> tuple[list[evolve.SurveyRow], list[str]]:
        seen["window_days"] = window_days
        return [], []

    monkeypatch.setattr(evolve, "gather", fake_gather)

    result = runner.invoke(
        app,
        [
            "evolve",
            "spec",
            "--dry-run",
            "--project",
            str(tmp_path),
            "--window-days",
            "7",
            "--min-runs",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert seen["window_days"] == 7
    assert "7d" in result.output


def test_evolve_dry_run_prints_notes_from_gather(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row(index=i) for i in range(6)]
    monkeypatch.setattr(
        evolve, "gather", lambda *args, **kwargs: (rows, ["CI run list hit the fetch limit"])
    )

    result = runner.invoke(app, ["evolve", "spec", "--dry-run", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "note: CI run list hit the fetch limit" in result.output


def test_evolve_dry_run_gather_command_error_exits_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_gather(*args: Any, **kwargs: Any) -> Any:
        raise CommandError("`gh run list` did not finish within 120s")

    monkeypatch.setattr(evolve, "gather", failing_gather)

    result = runner.invoke(app, ["evolve", "spec", "--dry-run", "--project", str(tmp_path)])

    assert result.exit_code == 1
    assert "did not finish within 120s" in result.stderr


def test_evolve_dry_run_survives_unreadable_workflows_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#242 regression: an unreadable `.github/workflows` must not crash `agent
    evolve --dry-run` (real `gather`/`caller_workflows`, nothing mocked out) —
    the `gather()` call site that checks for a "no evidence source" note is a
    second, previously-unguarded caller of `stubs.caller_workflows` besides
    `_fetch_ci_runs`.
    """
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    real_iterdir = Path.iterdir

    def fake_iterdir(self: Path) -> object:
        if self == workflows:
            raise PermissionError(13, "Permission denied", str(workflows))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)

    # "triage" has no other evidence source (not implement/pr, not in
    # LANE_ESCALATION_HEADERS), so it's the lane that reaches the previously
    # unguarded `caller_workflows` call in `gather`.
    result = runner.invoke(app, ["evolve", "triage", "--dry-run", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert result.exception is None
    assert "no evidence source" in result.output
