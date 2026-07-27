import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_ops import prompts
from agent_ops.runs import Outcome
from agent_ops.utils import CommandError
from agent_ops.workflows import evolve
from agent_ops.workflows.evolve import SurveyRow, baseline, build_survey

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


def test_fetch_ci_runs_fails_open_on_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [Path("triage.yml")])

    def boom(cmd: list[str], **kwargs: Any) -> Any:
        raise CommandError("`gh run list` did not finish within 120s")

    monkeypatch.setattr(evolve, "run", boom)

    assert evolve._fetch_ci_runs(tmp_path, "triage", 10) == ([], False)


def test_fetch_ci_runs_fails_open_on_missing_gh_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [Path("triage.yml")])

    def boom(cmd: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(evolve, "run", boom)

    assert evolve._fetch_ci_runs(tmp_path, "triage", 10) == ([], False)


def test_fetch_ci_runs_fails_open_on_bad_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evolve.stubs, "caller_workflows", lambda root, lane: [Path("triage.yml")])
    monkeypatch.setattr(evolve, "run", lambda cmd, **kwargs: _Proc("not json"))

    assert evolve._fetch_ci_runs(tmp_path, "triage", 10) == ([], False)


# --- _fetch_prs / _fetch_escalations fail open --------------------------------


def test_fetch_prs_fails_open_on_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(cmd: list[str], **kwargs: Any) -> Any:
        raise CommandError("`gh pr list` did not finish within 120s")

    monkeypatch.setattr(evolve, "run", boom)

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


def test_fetch_escalations_fails_open_on_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(cmd: list[str], **kwargs: Any) -> Any:
        raise CommandError("`gh issue list` did not finish within 120s")

    monkeypatch.setattr(evolve, "run", boom)

    assert evolve._fetch_escalations(tmp_path) == ([], False)


def test_fetch_escalations_fails_open_on_missing_gh_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(cmd: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(evolve, "run", boom)

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
