import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_ops import github, prompts, worktree
from agent_ops.runs import Outcome
from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult
from agent_ops.utils import (
    PLATFORM_ROOT,
    SPAWN_FAILURE_RETURNCODE,
    TIMEOUT_RETURNCODE,
    CommandError,
)
from agent_ops.workflows import evolve
from agent_ops.workflows.evolve import EvolveChange, NoopVerdict, SurveyRow, baseline, build_survey

NOW = datetime(2026, 7, 26, 0, 0, 0, tzinfo=UTC)
WINDOW_DAYS = 30
CUTOFF = NOW.replace(month=6, day=26)  # NOW - 30 days


def _ci_run(created: str, *, event: str = "schedule", conclusion: str = "success") -> dict:
    return {
        "databaseId": 1,
        "event": event,
        "status": "completed",
        "conclusion": conclusion,
        "createdAt": created,
        "updatedAt": created,
        "url": f"https://github.com/o/r/actions/runs/{created}",
    }


def _pr(
    branch: str,
    *,
    created: str,
    merged: str | None = None,
    closed: str | None = None,
    state: str = "OPEN",
    number: int = 1,
) -> dict:
    return {
        "number": number,
        "headRefName": branch,
        "state": state,
        "createdAt": created,
        "closedAt": closed,
        "mergedAt": merged,
        "url": f"https://github.com/o/r/pull/{number}",
    }


def _issue(*comments: dict, number: int = 5) -> dict:
    return {
        "number": number,
        "url": f"https://github.com/o/r/issues/{number}",
        "comments": list(comments),
    }


def _comment(body: str, created: str) -> dict:
    return {"body": body, "createdAt": created}


def _Proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr="")


# --- build_survey --------------------------------------------------------


def test_build_survey_ci_row() -> None:
    rows = build_survey(
        lane="spec",
        ci_runs=[_ci_run("2026-07-20T00:00:00Z", event="workflow_dispatch", conclusion="failure")],
        prs=[],
        issues=[],
        outcomes=[],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "ci"
    assert row.lane == "spec"
    assert row.trigger == "workflow_dispatch"
    assert row.conclusion == "failure"


def test_build_survey_pr_row_merged_and_lane_from_branch() -> None:
    rows = build_survey(
        lane="implement",
        ci_runs=[],
        prs=[_pr("fix/issue-42", created="2026-07-20T00:00:00Z", merged="2026-07-21T00:00:00Z")],
        issues=[],
        outcomes=[],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "pr"
    assert row.lane == "implement"
    assert row.conclusion == "merged"
    assert row.when == datetime(2026, 7, 21, tzinfo=UTC)  # mergedAt wins over createdAt


def test_build_survey_pr_row_generic_branch_is_not_implement_lane() -> None:
    rows = build_survey(
        lane="pr",
        ci_runs=[],
        prs=[_pr("feature/whatever", created="2026-07-20T00:00:00Z")],
        issues=[],
        outcomes=[],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert len(rows) == 1
    assert rows[0].lane == "pr"
    assert rows[0].conclusion == "open"

    # Querying "implement" must not pick up the generic-branch PR.
    assert (
        build_survey(
            lane="implement",
            ci_runs=[],
            prs=[_pr("feature/whatever", created="2026-07-20T00:00:00Z")],
            issues=[],
            outcomes=[],
            now=NOW,
            window_days=WINDOW_DAYS,
        )
        == []
    )


def test_build_survey_pr_row_closed_state() -> None:
    rows = build_survey(
        lane="implement",
        ci_runs=[],
        prs=[
            _pr(
                "fix/issue-9",
                created="2026-07-20T00:00:00Z",
                closed="2026-07-21T00:00:00Z",
                state="CLOSED",
            )
        ],
        issues=[],
        outcomes=[],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert rows[0].conclusion == "closed"


def test_build_survey_escalation_row_matches_header() -> None:
    rows = build_survey(
        lane="spec",
        ci_runs=[],
        prs=[],
        issues=[
            _issue(_comment("## Spec agent — escalation\n\nneeds a human", "2026-07-20T00:00:00Z"))
        ],
        outcomes=[],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert len(rows) == 1
    assert rows[0].source == "escalation"
    assert rows[0].conclusion == "escalated"


def test_build_survey_non_matching_comment_produces_no_row() -> None:
    rows = build_survey(
        lane="spec",
        ci_runs=[],
        prs=[],
        issues=[_issue(_comment("just a regular comment", "2026-07-20T00:00:00Z"))],
        outcomes=[],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert rows == []


def test_build_survey_escalation_header_only_applies_to_its_own_lane() -> None:
    # "spec"'s header must not surface when querying a lane with no header of its own.
    rows = build_survey(
        lane="triage",
        ci_runs=[],
        prs=[],
        issues=[
            _issue(_comment("## Spec agent — escalation\n\nneeds a human", "2026-07-20T00:00:00Z"))
        ],
        outcomes=[],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert rows == []


def test_build_survey_local_outcome_row() -> None:
    rows = build_survey(
        lane="implement",
        ci_runs=[],
        prs=[],
        issues=[],
        outcomes=[
            Outcome(
                state="done", pr_url="https://x/pull/9", reason=None, finished_at=NOW.timestamp()
            )
        ],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert len(rows) == 1
    assert rows[0].source == "local"
    assert rows[0].conclusion == "done"
    assert rows[0].url == "https://x/pull/9"


def test_build_survey_local_outcome_with_no_finished_at_produces_no_row() -> None:
    rows = build_survey(
        lane="implement",
        ci_runs=[],
        prs=[],
        issues=[],
        outcomes=[Outcome(state="done", pr_url=None, reason=None, finished_at=None)],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert rows == []


def test_build_survey_window_boundary_exact_edge_is_kept() -> None:
    rows = build_survey(
        lane="spec",
        ci_runs=[_ci_run(CUTOFF.strftime("%Y-%m-%dT%H:%M:%SZ"))],
        prs=[],
        issues=[],
        outcomes=[],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert len(rows) == 1


def test_build_survey_window_boundary_one_second_past_is_dropped() -> None:
    just_outside = CUTOFF - timedelta(seconds=1)
    rows = build_survey(
        lane="spec",
        ci_runs=[_ci_run(just_outside.strftime("%Y-%m-%dT%H:%M:%SZ"))],
        prs=[],
        issues=[],
        outcomes=[],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert rows == []


def test_build_survey_unparseable_timestamp_is_dropped_not_raised() -> None:
    rows = build_survey(
        lane="spec",
        ci_runs=[_ci_run("not-a-timestamp")],
        prs=[],
        issues=[],
        outcomes=[],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert rows == []


def test_build_survey_sorted_newest_first() -> None:
    rows = build_survey(
        lane="spec",
        ci_runs=[_ci_run("2026-07-10T00:00:00Z"), _ci_run("2026-07-20T00:00:00Z")],
        prs=[],
        issues=[],
        outcomes=[],
        now=NOW,
        window_days=WINDOW_DAYS,
    )
    assert [r.when for r in rows] == [
        datetime(2026, 7, 20, tzinfo=UTC),
        datetime(2026, 7, 10, tzinfo=UTC),
    ]


# --- baseline --------------------------------------------------------------


def _row(source: str, conclusion: str, when: datetime = NOW) -> SurveyRow:
    return SurveyRow(
        when=when,
        lane="spec",
        trigger="t",
        conclusion=conclusion,
        duration="-",
        url="u",
        source=source,
    )


def test_baseline_zero_runs_has_zero_failure_rate() -> None:
    b = baseline([], lane="spec", window_days=30)
    assert b.runs == 0
    assert b.ci_failure_rate == 0.0
    assert b.ci_failures == 0


def test_baseline_ci_failure_rate_over_failed_conclusions() -> None:
    rows = [
        _row("ci", "success"),
        _row("ci", "failure"),
        _row("ci", "startup_failure"),
        _row("ci", "cancelled"),
        _row("ci", "timed_out"),
    ]
    b = baseline(rows, lane="spec", window_days=30)
    assert b.runs == 5
    assert b.ci_failures == 4
    assert b.ci_failure_rate == 0.8


def test_baseline_counts_escalations_and_pr_merged_vs_closed() -> None:
    rows = [
        _row("escalation", "escalated"),
        _row("escalation", "escalated"),
        _row("pr", "merged"),
        _row("pr", "merged"),
        _row("pr", "closed"),
        _row("pr", "open"),  # neither merged nor closed
    ]
    b = baseline(rows, lane="implement", window_days=30)
    assert b.escalations == 2
    assert b.prs_merged == 2
    assert b.prs_closed == 1
    assert b.runs == 4  # escalation rows annotate a run, they are not one


# --- _fetch_ci_runs ----------------------------------------------------------


def test_fetch_ci_runs_returns_empty_and_never_shells_out_when_no_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [])

    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("gh must not be called when the lane has no caller workflow")

    monkeypatch.setattr(evolve, "run", unreachable)

    assert evolve._fetch_ci_runs(tmp_path, "implement", 10) == ([], False)


def test_fetch_ci_runs_queries_every_caller_and_merges_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        evolve.stubs,
        "caller_workflows",
        lambda root, lane: [Path("triage.yml"), Path("triage-nightly.yml")],
    )

    def fake_run(cmd: list[str], **kwargs: Any) -> Any:
        workflow = cmd[cmd.index("--workflow") + 1]
        run = _ci_run("2026-07-20T00:00:00Z")
        run["databaseId"] = 1 if workflow == "triage.yml" else 2
        return _Proc(json.dumps([run]))

    monkeypatch.setattr(evolve, "run", fake_run)

    runs, truncated = evolve._fetch_ci_runs(tmp_path, "triage", 10)

    assert len(runs) == 2
    assert not truncated


def test_fetch_ci_runs_flags_truncation_when_a_caller_hits_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [Path("triage.yml")])
    monkeypatch.setattr(
        evolve, "run", lambda cmd, **kwargs: _Proc(json.dumps([_ci_run("2026-07-20T00:00:00Z")]))
    )

    runs, truncated = evolve._fetch_ci_runs(tmp_path, "triage", 1)

    assert len(runs) == 1
    assert truncated


def test_fetch_ci_runs_fails_open_on_command_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `check=False` never raises (agent-ops#154) — a timeout comes back as a
    # synthetic non-zero `CompletedProcess`, same as any other `gh` failure.
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [Path("triage.yml")])
    monkeypatch.setattr(
        evolve, "run", lambda cmd, **kwargs: _Proc("", returncode=TIMEOUT_RETURNCODE)
    )

    assert evolve._fetch_ci_runs(tmp_path, "triage", 10) == ([], False)


def test_fetch_ci_runs_fails_open_on_missing_gh_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [Path("triage.yml")])
    monkeypatch.setattr(
        evolve, "run", lambda cmd, **kwargs: _Proc("", returncode=SPAWN_FAILURE_RETURNCODE)
    )

    assert evolve._fetch_ci_runs(tmp_path, "triage", 10) == ([], False)


def test_fetch_ci_runs_fails_open_on_bad_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [Path("triage.yml")])
    monkeypatch.setattr(evolve, "run", lambda cmd, **kwargs: _Proc("not json"))

    assert evolve._fetch_ci_runs(tmp_path, "triage", 10) == ([], False)


# --- _fetch_prs / _fetch_escalations fail open --------------------------------


def test_fetch_prs_fails_open_on_command_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        evolve, "run", lambda cmd, **kwargs: _Proc("", returncode=TIMEOUT_RETURNCODE)
    )

    assert evolve._fetch_prs(tmp_path, 10) == ([], False)


def test_fetch_prs_flags_truncation_when_the_result_hits_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        evolve,
        "run",
        lambda cmd, **kwargs: _Proc(
            json.dumps([_pr("fix/issue-1", created="2026-07-20T00:00:00Z")])
        ),
    )

    prs, truncated = evolve._fetch_prs(tmp_path, 1)

    assert len(prs) == 1
    assert truncated


def test_fetch_escalations_fails_open_on_command_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        evolve, "run", lambda cmd, **kwargs: _Proc("", returncode=TIMEOUT_RETURNCODE)
    )

    assert evolve._fetch_escalations(tmp_path) == ([], False)


def test_fetch_escalations_fails_open_on_missing_gh_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        evolve, "run", lambda cmd, **kwargs: _Proc("", returncode=SPAWN_FAILURE_RETURNCODE)
    )

    assert evolve._fetch_escalations(tmp_path) == ([], False)


def test_fetch_escalations_flags_truncation_when_the_result_hits_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve, "run", lambda cmd, **kwargs: _Proc(json.dumps([_issue()])))

    issues, truncated = evolve._fetch_escalations(tmp_path, limit=1)

    assert len(issues) == 1
    assert truncated


# --- baseline: implement double-count --------------------------------------


def test_baseline_dedupes_local_row_sharing_url_with_pr_row() -> None:
    rows = [
        SurveyRow(
            when=NOW,
            lane="implement",
            trigger="pr",
            conclusion="merged",
            duration="-",
            url="https://x/pull/9",
            source="pr",
        ),
        SurveyRow(
            when=NOW,
            lane="implement",
            trigger="local",
            conclusion="done",
            duration="-",
            url="https://x/pull/9",
            source="local",
        ),
    ]
    b = baseline(rows, lane="implement", window_days=30)
    assert b.runs == 1


def test_baseline_does_not_double_count_a_ci_run_and_its_escalation() -> None:
    rows = [
        SurveyRow(
            when=NOW,
            lane="spec",
            trigger="workflow_dispatch",
            conclusion="failure",
            duration="1h00m",
            url="https://github.com/o/r/actions/runs/1",
            source="ci",
        ),
        SurveyRow(
            when=NOW,
            lane="spec",
            trigger="issue",
            conclusion="escalated",
            duration="-",
            url="https://github.com/o/r/issues/5",
            source="escalation",
        ),
    ]
    b = baseline(rows, lane="spec", window_days=30)
    assert b.runs == 1
    assert b.escalations == 1


def test_baseline_keeps_local_row_with_no_matching_pr() -> None:
    rows = [
        SurveyRow(
            when=NOW,
            lane="implement",
            trigger="local",
            conclusion="done",
            duration="-",
            url="",
            source="local",
        ),
    ]
    b = baseline(rows, lane="implement", window_days=30)
    assert b.runs == 1


# --- gather: skips useless fetches per lane ---------------------------------


def _unreachable(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("must not be called for this lane")


def test_gather_skips_pr_and_escalation_and_local_fetches_for_triage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve, "_fetch_ci_runs", lambda root, lane, limit: ([], False))
    monkeypatch.setattr(evolve, "_fetch_prs", _unreachable)
    monkeypatch.setattr(evolve, "_fetch_escalations", _unreachable)
    monkeypatch.setattr(evolve, "_load_local_outcomes", _unreachable)
    # A caller workflow exists in this repo, so the "no evidence source" note
    # (tested separately below) does not apply here.
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [Path("triage.yml")])

    rows, notes = evolve.gather(tmp_path, "triage", now=NOW, window_days=WINDOW_DAYS)

    assert rows == []
    assert notes == []


def test_gather_fetches_prs_and_local_outcomes_for_implement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve, "_fetch_ci_runs", lambda root, lane, limit: ([], False))
    monkeypatch.setattr(evolve, "_fetch_prs", lambda root, limit: ([], False))
    monkeypatch.setattr(evolve, "_fetch_escalations", _unreachable)
    called: dict[str, bool] = {}
    monkeypatch.setattr(
        evolve, "_load_local_outcomes", lambda root: called.setdefault("local", True) and []
    )

    evolve.gather(tmp_path, "implement", now=NOW, window_days=WINDOW_DAYS)

    assert called.get("local") is True


def test_gather_fetches_escalations_for_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve, "_fetch_ci_runs", lambda root, lane, limit: ([], False))
    monkeypatch.setattr(evolve, "_fetch_prs", _unreachable)
    called: dict[str, bool] = {}
    monkeypatch.setattr(
        evolve,
        "_fetch_escalations",
        lambda root: (called.setdefault("escalations", True) and [], False),
    )
    monkeypatch.setattr(evolve, "_load_local_outcomes", _unreachable)

    evolve.gather(tmp_path, "spec", now=NOW, window_days=WINDOW_DAYS)

    assert called.get("escalations") is True


def test_gather_surfaces_a_note_when_ci_fetch_is_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve, "_fetch_ci_runs", lambda root, lane, limit: ([], True))
    monkeypatch.setattr(evolve, "_fetch_prs", _unreachable)
    monkeypatch.setattr(evolve, "_fetch_escalations", _unreachable)
    monkeypatch.setattr(evolve, "_load_local_outcomes", _unreachable)
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [Path("triage.yml")])

    rows, notes = evolve.gather(tmp_path, "triage", now=NOW, window_days=WINDOW_DAYS)

    assert len(notes) == 1
    assert "fetch limit" in notes[0]


def test_gather_surfaces_a_note_when_pr_fetch_is_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve, "_fetch_ci_runs", lambda root, lane, limit: ([], False))
    monkeypatch.setattr(evolve, "_fetch_prs", lambda root, limit: ([], True))
    monkeypatch.setattr(evolve, "_fetch_escalations", _unreachable)
    monkeypatch.setattr(evolve, "_load_local_outcomes", lambda root: [])

    rows, notes = evolve.gather(tmp_path, "implement", now=NOW, window_days=WINDOW_DAYS)

    assert len(notes) == 1
    assert "PR list hit" in notes[0]


def test_gather_surfaces_a_note_when_escalation_fetch_is_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve, "_fetch_ci_runs", lambda root, lane, limit: ([], False))
    monkeypatch.setattr(evolve, "_fetch_prs", _unreachable)
    monkeypatch.setattr(evolve, "_fetch_escalations", lambda root: ([], True))
    monkeypatch.setattr(evolve, "_load_local_outcomes", _unreachable)

    rows, notes = evolve.gather(tmp_path, "spec", now=NOW, window_days=WINDOW_DAYS)

    assert len(notes) == 1
    assert "issue list hit" in notes[0]


def test_gather_notes_review_lane_has_no_evidence_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve, "_fetch_ci_runs", lambda root, lane, limit: ([], False))
    monkeypatch.setattr(evolve, "_fetch_prs", _unreachable)
    monkeypatch.setattr(evolve, "_fetch_escalations", _unreachable)
    monkeypatch.setattr(evolve, "_load_local_outcomes", _unreachable)
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [])

    rows, notes = evolve.gather(tmp_path, "review", now=NOW, window_days=WINDOW_DAYS)

    assert rows == []
    assert len(notes) == 1
    assert "no evidence source" in notes[0]


def test_gather_does_not_note_missing_source_when_a_caller_workflow_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve, "_fetch_ci_runs", lambda root, lane, limit: ([], False))
    monkeypatch.setattr(evolve, "_fetch_prs", _unreachable)
    monkeypatch.setattr(evolve, "_fetch_escalations", _unreachable)
    monkeypatch.setattr(evolve, "_load_local_outcomes", _unreachable)
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [Path("triage.yml")])

    rows, notes = evolve.gather(tmp_path, "triage", now=NOW, window_days=WINDOW_DAYS)

    assert notes == []


def test_gather_does_not_note_missing_source_for_implement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve, "_fetch_ci_runs", lambda root, lane, limit: ([], False))
    monkeypatch.setattr(evolve, "_fetch_prs", lambda root, limit: ([], False))
    monkeypatch.setattr(evolve, "_fetch_escalations", _unreachable)
    monkeypatch.setattr(evolve, "_load_local_outcomes", lambda root: [])
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [])

    rows, notes = evolve.gather(tmp_path, "implement", now=NOW, window_days=WINDOW_DAYS)

    assert notes == []


# --- prompts.task_names ------------------------------------------------------


def test_task_names_contains_spec_not_orchestrator() -> None:
    names = prompts.task_names()
    assert "spec" in names
    assert "orchestrator" not in names


# --- _duration ---------------------------------------------------------------


def test_duration_missing_start_is_dash() -> None:
    assert evolve._duration(None, NOW) == "-"


def test_duration_missing_end_is_dash() -> None:
    assert evolve._duration(NOW, None) == "-"


def test_duration_negative_span_is_dash() -> None:
    assert evolve._duration(NOW, NOW - timedelta(minutes=5)) == "-"


def test_duration_under_a_minute() -> None:
    assert evolve._duration(NOW, NOW + timedelta(seconds=30)) == "<1m"


def test_duration_minutes_only() -> None:
    assert evolve._duration(NOW, NOW + timedelta(minutes=5)) == "5m"


def test_duration_hours_and_minutes() -> None:
    assert evolve._duration(NOW, NOW + timedelta(hours=2, minutes=9)) == "2h09m"


def test_duration_multi_day() -> None:
    assert evolve._duration(NOW, NOW + timedelta(days=3, hours=4)) == "3d4h"


# --- _load_local_outcomes -----------------------------------------------------


def test_load_local_outcomes_returns_empty_when_no_agent_runs_dir(tmp_path: Path) -> None:
    assert evolve._load_local_outcomes(tmp_path) == []


def test_load_local_outcomes_reads_matching_outcome_files(tmp_path: Path) -> None:
    from agent_ops.runs import write_outcome

    write_outcome(tmp_path, 9, state="done", pr_url="https://x/pull/9")
    write_outcome(tmp_path, 42, state="failed", reason="gate failed")

    outcomes = evolve._load_local_outcomes(tmp_path)

    assert {o.state for o in outcomes} == {"done", "failed"}


def test_load_local_outcomes_skips_non_matching_filename(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".agent-runs"
    runs_dir.mkdir()
    (runs_dir / "issue-9-feedback.md").write_text("not an outcome file")

    assert evolve._load_local_outcomes(tmp_path) == []


def test_load_local_outcomes_skips_unreadable_record(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".agent-runs"
    runs_dir.mkdir()
    (runs_dir / "issue-9-outcome.json").write_text("not json")

    assert evolve._load_local_outcomes(tmp_path) == []


# --- parse_evolve --------------------------------------------------------------


def test_parse_evolve_no_marker_is_none() -> None:
    assert evolve.parse_evolve("no block here") is None


def test_parse_evolve_marker_with_empty_tail_is_none() -> None:
    assert evolve.parse_evolve("EVOLVE VERDICT:\n\n") is None


def test_parse_evolve_none_with_reason_is_noop_verdict() -> None:
    result = evolve.parse_evolve(
        "reviewed the survey\n\nEVOLVE VERDICT:\nnone — only 3 runs, too thin to act on\n"
    )
    assert result == NoopVerdict("only 3 runs, too thin to act on")


def test_parse_evolve_bare_none_with_no_reason_raises() -> None:
    with pytest.raises(ValueError, match="no reason"):
        evolve.parse_evolve("EVOLVE VERDICT:\nnone\n")


def test_parse_evolve_single_change() -> None:
    text = (
        "EVOLVE VERDICT:\n"
        "drift — stop re-reading closed issues — https://github.com/o/r/actions/runs/1\n"
    )
    result = evolve.parse_evolve(text)
    assert result == [
        EvolveChange(
            "drift", "stop re-reading closed issues", "https://github.com/o/r/actions/runs/1"
        )
    ]


def test_parse_evolve_multiple_changes() -> None:
    text = (
        "EVOLVE VERDICT:\n"
        "vagueness — pin the escalation wording — #150\n"
        "fuzzy gate — only speak above 2 failures — https://github.com/o/r/actions/runs/2\n"
    )
    result = evolve.parse_evolve(text)
    assert result == [
        EvolveChange("vagueness", "pin the escalation wording", "#150"),
        EvolveChange(
            "fuzzy gate", "only speak above 2 failures", "https://github.com/o/r/actions/runs/2"
        ),
    ]


def test_parse_evolve_unknown_failure_mode_raises() -> None:
    text = "EVOLVE VERDICT:\nperformance — speed it up — #150\n"
    with pytest.raises(ValueError, match="not a named failure mode"):
        evolve.parse_evolve(text)


def test_parse_evolve_change_with_no_citation_raises() -> None:
    text = "EVOLVE VERDICT:\ndrift — stop doing the thing — trust me\n"
    with pytest.raises(ValueError, match="cites no run URL"):
        evolve.parse_evolve(text)


def test_parse_evolve_line_with_no_separators_raises() -> None:
    text = "EVOLVE VERDICT:\njust some prose with no structure at all\n"
    with pytest.raises(ValueError, match="unparseable"):
        evolve.parse_evolve(text)


def test_parse_evolve_uses_last_marker() -> None:
    text = "EVOLVE VERDICT:\ndrift — draft attempt — #1\nEVOLVE VERDICT:\nnone — changed my mind\n"
    assert evolve.parse_evolve(text) == NoopVerdict("changed my mind")


def test_parse_evolve_summary_with_internal_dash_is_not_truncated() -> None:
    """A summary containing its own spaced dash must not spill into citations.

    A naive `split(maxsplit=2)` stops after the first two separators, so a
    summary like "retries were added - even though the prompt didn't ask"
    would spill its tail into the citations field and garble the PR body.
    """
    text = (
        "EVOLVE VERDICT:\n"
        "drift — retries were added - even though the prompt didn't ask - see #123\n"
    )
    result = evolve.parse_evolve(text)
    assert result == [
        EvolveChange(
            "drift",
            "retries were added - even though the prompt didn't ask",
            "see #123",
        )
    ]


# --- run_evolve ------------------------------------------------------------


def _survey_rows(n: int = 6, *, lane: str = "spec") -> list[SurveyRow]:
    return [
        SurveyRow(
            when=NOW,
            lane=lane,
            trigger="schedule",
            conclusion="success",
            duration="-",
            url=f"https://github.com/o/r/actions/runs/{i}",
            source="ci",
        )
        for i in range(n)
    ]


class _FakeEvolveRuntime:
    name = "fake"

    def __init__(self, text: str) -> None:
        self.text = text

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest) -> RunResult:
        return RunResult(ok=True, text=self.text)

    def classify_failure(self, result: RunResult) -> FailureKind:
        return FailureKind.AGENT_FAILURE


class _FakeEvolveProc:
    def __init__(self, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _stub_run_evolve(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    lane: str = "spec",
    verdict_text: str,
    diff_output: str = "",
    rows: list[SurveyRow] | None = None,
) -> dict[str, Any]:
    """Wire run_evolve's collaborators with fakes; return captured calls/state."""
    (tmp_path / "prompts" / "tasks").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "tasks" / f"{lane}.md").write_text("# existing prompt\n")
    wt_path = tmp_path / ".worktrees" / f"evolve-{lane}-tmp"
    wt_path.mkdir(parents=True, exist_ok=True)

    calls: list[list[str]] = []
    captured: dict[str, Any] = {"calls": calls}

    def fake_run(cmd: list[str], **kwargs: Any) -> _FakeEvolveProc:
        calls.append(cmd)
        if cmd[:2] == ["git", "status"]:
            paths = [p for p in diff_output.splitlines() if p]
            porcelain = "".join(f" M {p}\0" for p in paths)
            return _FakeEvolveProc(porcelain)
        if cmd[:3] == ["git", "rev-parse", "--short"]:
            return _FakeEvolveProc("abc1234\n")
        return _FakeEvolveProc("")

    def fake_role_request(
        config: Any, role_name: str, prompt: str, cwd: Path, **kwargs: Any
    ) -> tuple[object, RunRequest]:
        return _FakeEvolveRuntime(verdict_text), RunRequest(prompt=prompt, cwd=cwd)

    def fake_sync_labels(
        project_root: Path, labels: dict[str, github.Label], *, repo: str | None = None
    ) -> github.LabelSync:
        captured["synced_labels"] = labels
        return github.LabelSync(created=list(labels), updated=[], unchanged=[], failed=[])

    def fake_create_pr(
        cwd: Path, *, base: str, title: str, body: str, draft: bool = False, labels: Any = ()
    ) -> str:
        captured["pr"] = {
            "cwd": cwd,
            "base": base,
            "title": title,
            "body": body,
            "draft": draft,
            "labels": labels,
        }
        return "https://github.com/acme/widgets/pull/9"

    monkeypatch.setattr(evolve, "run", fake_run)
    monkeypatch.setattr(worktree, "run", fake_run)
    monkeypatch.setattr(evolve, "role_request", fake_role_request)
    fallback_rows = rows if rows is not None else _survey_rows(lane=lane)
    monkeypatch.setattr(evolve, "gather", lambda *a, **k: (fallback_rows, []))

    def fake_remove(*args: Any, **kwargs: Any) -> None:
        captured["removed"] = True
        captured["remove_kwargs"] = kwargs

    monkeypatch.setattr(worktree, "create_detached", lambda *a, **k: wt_path)
    monkeypatch.setattr(worktree, "remove", fake_remove)
    monkeypatch.setattr(github, "sync_labels", fake_sync_labels)
    monkeypatch.setattr(github, "remote_slug", lambda root: "acme/widgets")
    monkeypatch.setattr(github, "create_pr", fake_create_pr)
    monkeypatch.setattr(github, "open_prs", lambda *a, **k: [])
    return captured


def test_run_evolve_missing_task_file_raises_and_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no worktree should be created for a lane with no prompt file")

    monkeypatch.setattr(worktree, "create_detached", unreachable)

    with pytest.raises(RuntimeError, match="no prompts/tasks/ghost.md"):
        evolve.run_evolve(tmp_path, "ghost")


def test_run_evolve_below_min_runs_is_a_cheap_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "prompts" / "tasks").mkdir(parents=True)
    (tmp_path / "prompts" / "tasks" / "spec.md").write_text("# existing prompt\n")
    monkeypatch.setattr(evolve, "gather", lambda *a, **k: (_survey_rows(2), []))

    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("below min_runs must return before spawning any agent")

    monkeypatch.setattr(worktree, "create_detached", unreachable)
    logged: list[str] = []

    result = evolve.run_evolve(tmp_path, "spec", min_runs=5, log=logged.append)

    assert result == []
    assert any("no-op" in line for line in logged)


def test_run_evolve_open_pr_for_lane_short_circuits_to_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second evolve PR must not stack on an unreviewed first for the same lane."""
    (tmp_path / "prompts" / "tasks").mkdir(parents=True)
    (tmp_path / "prompts" / "tasks" / "spec.md").write_text("# existing prompt\n")
    monkeypatch.setattr(evolve, "gather", lambda *a, **k: (_survey_rows(6), []))
    monkeypatch.setattr(
        github,
        "open_prs",
        lambda *a, **k: [
            {
                "number": 7,
                "url": "https://github.com/o/r/pull/7",
                "headRefName": "evolve/spec-abc1234",
            }
        ],
    )

    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an already-open evolve PR for this lane must short-circuit")

    monkeypatch.setattr(github, "sync_labels", unreachable)
    monkeypatch.setattr(worktree, "create_detached", unreachable)
    logged: list[str] = []

    result = evolve.run_evolve(tmp_path, "spec", log=logged.append)

    assert result == []
    assert any("#7" in line for line in logged)


def test_run_evolve_stale_pr_check_fails_closed_before_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `gh` failure checking for a stale PR must abort, not fail open.

    Failing open here would still spend a worktree, an agent run, a commit,
    and a push before dying at `create_pr` on the same underlying `gh`
    failure — same reasoning as distill's stale-PR guard (agent-ops#175).
    """
    (tmp_path / "prompts" / "tasks").mkdir(parents=True)
    (tmp_path / "prompts" / "tasks" / "spec.md").write_text("# existing prompt\n")
    monkeypatch.setattr(evolve, "gather", lambda *a, **k: (_survey_rows(6), []))

    def broken_open_prs(*args: Any, **kwargs: Any) -> Any:
        raise CommandError("gh: not authenticated")

    monkeypatch.setattr(github, "open_prs", broken_open_prs)

    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a failed stale-PR check must abort before any label sync")

    monkeypatch.setattr(github, "sync_labels", unreachable)
    monkeypatch.setattr(worktree, "create_detached", unreachable)

    with pytest.raises(RuntimeError, match="could not check for a stale evolve PR"):
        evolve.run_evolve(tmp_path, "spec")


def test_run_evolve_failed_label_sync_aborts_before_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A label that fails to sync must abort before any worktree/branch exists.

    `human-merge-only` is the containment mechanism for the draft PR this run
    would open — continuing past a sync failure risks a push and a PR with no
    label to keep it human-merge-only, leaving a dangling branch behind.
    """
    (tmp_path / "prompts" / "tasks").mkdir(parents=True)
    (tmp_path / "prompts" / "tasks" / "spec.md").write_text("# existing prompt\n")
    monkeypatch.setattr(evolve, "gather", lambda *a, **k: (_survey_rows(6), []))
    monkeypatch.setattr(github, "open_prs", lambda *a, **k: [])
    monkeypatch.setattr(github, "remote_slug", lambda root: "acme/widgets")
    monkeypatch.setattr(
        github,
        "sync_labels",
        lambda *a, **k: github.LabelSync(
            created=[], updated=[], unchanged=[], failed=[("human-merge-only", "no write scope")]
        ),
    )

    def unreachable(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no worktree should be created when the label sync failed")

    monkeypatch.setattr(worktree, "create_detached", unreachable)

    with pytest.raises(RuntimeError, match="could not sync the human-merge-only label"):
        evolve.run_evolve(tmp_path, "spec")


def test_run_evolve_noop_verdict_opens_no_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_run_evolve(
        monkeypatch, tmp_path, verdict_text="EVOLVE VERDICT:\nnone — nothing repeatable yet\n"
    )
    logged: list[str] = []

    result = evolve.run_evolve(tmp_path, "spec", log=logged.append)

    assert result == []
    assert "pr" not in captured
    assert not any(c[:2] == ["git", "push"] for c in captured["calls"])
    assert captured.get("removed") is True
    assert captured["remove_kwargs"].get("delete_branch") is True
    assert any("nothing repeatable yet" in line for line in logged)


def test_run_evolve_unparseable_verdict_raises_not_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_run_evolve(monkeypatch, tmp_path, verdict_text="no verdict block at all")

    with pytest.raises(RuntimeError, match="no parseable verdict"):
        evolve.run_evolve(tmp_path, "spec")

    assert "pr" not in captured
    assert captured.get("removed") is True


def test_run_evolve_malformed_verdict_raises_not_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_run_evolve(
        monkeypatch, tmp_path, verdict_text="EVOLVE VERDICT:\nperformance — speed it up — #1\n"
    )

    with pytest.raises(RuntimeError, match="unparseable verdict"):
        evolve.run_evolve(tmp_path, "spec")


def test_run_evolve_diff_outside_allowlist_aborts_before_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_run_evolve(
        monkeypatch,
        tmp_path,
        verdict_text="EVOLVE VERDICT:\ndrift — tighten it — #1\n",
        diff_output="prompts/orchestrator.md\n",
    )

    with pytest.raises(RuntimeError, match="disallowed path"):
        evolve.run_evolve(tmp_path, "spec")

    assert "pr" not in captured
    assert not any(c[:2] == ["git", "push"] for c in captured["calls"])


def test_run_evolve_change_verdict_with_empty_diff_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_run_evolve(
        monkeypatch,
        tmp_path,
        verdict_text="EVOLVE VERDICT:\ndrift — tighten it — #1\n",
        diff_output="",
    )

    with pytest.raises(RuntimeError, match="unchanged"):
        evolve.run_evolve(tmp_path, "spec")

    assert "pr" not in captured


def test_run_evolve_noop_verdict_with_stray_edit_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_run_evolve(
        monkeypatch,
        tmp_path,
        verdict_text="EVOLVE VERDICT:\nnone — nothing to do\n",
        diff_output="prompts/tasks/spec.md\n",
    )

    with pytest.raises(RuntimeError, match="changed anyway"):
        evolve.run_evolve(tmp_path, "spec")

    assert "pr" not in captured


def test_run_evolve_happy_path_opens_draft_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_run_evolve(
        monkeypatch,
        tmp_path,
        verdict_text=(
            "EVOLVE VERDICT:\n"
            "drift — stop re-reading closed issues — https://github.com/o/r/actions/runs/1\n"
        ),
        diff_output="prompts/tasks/spec.md\n",
    )

    result = evolve.run_evolve(tmp_path, "spec")

    assert result == [
        EvolveChange(
            "drift",
            "stop re-reading closed issues",
            "https://github.com/o/r/actions/runs/1",
        )
    ]
    assert captured["pr"]["draft"] is True
    assert captured["pr"]["labels"] == ("human-merge-only",)
    assert "drift" in captured["pr"]["body"]
    assert "stop re-reading closed issues" in captured["pr"]["body"]
    assert "https://github.com/o/r/actions/runs/1" in captured["pr"]["body"]
    assert "baseline for spec" in captured["pr"]["body"]
    assert captured["synced_labels"] and "human-merge-only" in captured["synced_labels"]
    assert any(c[:3] == ["git", "checkout", "-b"] for c in captured["calls"])
    push_calls = [c for c in captured["calls"] if c[:2] == ["git", "push"]]
    assert push_calls and push_calls[0][-1].startswith("evolve/spec-")
    commit_calls = [c for c in captured["calls"] if c[:2] == ["git", "commit"]]
    assert commit_calls and commit_calls[0][-2:] == ["--", "prompts/tasks/spec.md"]
    assert captured.get("removed") is True
    assert captured["remove_kwargs"].get("delete_branch") is True


def test_evolve_pipeline_configures_git_identity_before_running_evolve() -> None:
    """`run_evolve` commits and pushes (see the `git commit`/`git push` calls
    asserted above), but GitHub-hosted runners set neither `user.name` nor
    `user.email`. Without a configured identity here, a real run would check
    out, install uv and the Claude Code CLI, survey a lane — and only then die
    at commit with "unable to auto-detect email address"."""
    text = (PLATFORM_ROOT / ".github" / "workflows" / "evolve-pipeline.yml").read_text()
    identity_pos = text.index("git config --global user.email")
    run_pos = text.index("uv run agent evolve")
    assert identity_pos < run_pos


def test_run_evolve_worktree_removed_even_when_run_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _stub_run_evolve(monkeypatch, tmp_path, verdict_text="garbage, no marker")

    with pytest.raises(RuntimeError):
        evolve.run_evolve(tmp_path, "spec")

    assert captured.get("removed") is True
    assert captured["remove_kwargs"].get("delete_branch") is True
