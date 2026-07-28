from __future__ import annotations

from typing import Any

import pytest

from agent_ops import runs, status
from agent_ops.claims import CLAIM_LABEL
from agent_ops.registry import RegistryConfig
from agent_ops.tui import data
from agent_ops.utils import CommandError


def _issue(number: int, created: str, *labels: str) -> dict[str, Any]:
    return {"number": number, "createdAt": created, "labels": [{"name": n} for n in labels]}


def _pr(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": "x",
        "baseRefName": "main",
        "headRefName": f"fix/issue-{number}",
    }


# --- load_repo / load_fleet -------------------------------------------------


def test_load_repo_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(repo: str) -> list[Any]:
        raise CommandError("nope")

    monkeypatch.setattr(status, "_pipeline_issues", boom)
    assert data.load_repo("o/a") == data.RepoData("o/a", readable=False)


def test_load_repo_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    issues = [_issue(1, "2026-01-01T00:00:00Z", "agent-ready")]
    prs = [_pr(9)]
    monkeypatch.setattr(status, "_pipeline_issues", lambda repo: issues)
    monkeypatch.setattr(status, "_open_prs", lambda repo: prs)
    result = data.load_repo("o/a")
    assert result.readable
    assert result.issues == issues
    assert result.prs == prs
    assert not result.truncated


def test_load_repo_pr_failure_degrades_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(repo: str) -> list[Any]:
        raise CommandError("nope")

    monkeypatch.setattr(status, "_pipeline_issues", lambda repo: [])
    monkeypatch.setattr(status, "_open_prs", boom)
    result = data.load_repo("o/a")
    assert result.readable
    assert result.prs == []


def test_load_fleet_skips_lane_check_when_nothing_could_be_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `needs-human` isn't in STAGE_CONSUMERS, so no repo could be unserviced —
    # the extra workflow-listing call must not happen.
    issues = [_issue(1, "2026-01-01T00:00:00Z", "needs-human")]
    monkeypatch.setattr(status, "_pipeline_issues", lambda repo: issues)
    monkeypatch.setattr(status, "_open_prs", lambda repo: [])

    def unreachable(repo: str) -> None:
        raise AssertionError("lanes_for should not be called")

    monkeypatch.setattr(data, "lanes_for", unreachable)
    fleet = data.load_fleet(RegistryConfig(repos=["o/a"]))
    assert fleet[0].lanes is None


def test_load_fleet_checks_lanes_when_a_consumer_stage_has_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issues = [_issue(1, "2026-01-01T00:00:00Z")]  # untriaged
    monkeypatch.setattr(status, "_pipeline_issues", lambda repo: issues)
    monkeypatch.setattr(status, "_open_prs", lambda repo: [])
    monkeypatch.setattr(data, "lanes_for", lambda repo: {"triage": status.LaneInfo(None, None)})
    fleet = data.load_fleet(RegistryConfig(repos=["o/a"]))
    assert fleet[0].lanes == {"triage": status.LaneInfo(None, None)}


def test_load_fleet_unreadable_repo_never_asks_for_lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(repo: str) -> list[Any]:
        raise CommandError("nope")

    monkeypatch.setattr(status, "_pipeline_issues", boom)

    def unreachable(repo: str) -> None:
        raise AssertionError("lanes_for should not be called for an unreadable repo")

    monkeypatch.setattr(data, "lanes_for", unreachable)
    fleet = data.load_fleet(RegistryConfig(repos=["o/a"]))
    assert fleet[0] == data.FleetRepo("o/a", data.RepoData("o/a", readable=False), None)


# --- lanes_for ---------------------------------------------------------------


def test_lanes_for_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(repo: str) -> dict[str, str]:
        raise CommandError("nope")

    monkeypatch.setattr(status, "_repo_workflows", boom)
    assert data.lanes_for("o/a") is None


def test_lanes_for_readable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status, "_repo_workflows", lambda repo: {"triage.yml": "..."})
    monkeypatch.setattr(
        status, "detect_lanes", lambda workflows: {"triage": status.LaneInfo(None, None)}
    )
    assert data.lanes_for("o/a") == {"triage": status.LaneInfo(None, None)}


# --- repo_summary --------------------------------------------------------------


def test_repo_summary_unreadable() -> None:
    fr = data.FleetRepo("o/a", data.RepoData("o/a", readable=False), None)
    assert data.repo_summary(fr) == data.RepoSummary(
        "o/a", readable=False, total_open=None, truncated=False, unserviced=False
    )


def test_repo_summary_unserviced_marker() -> None:
    issues = [_issue(1, "2026-01-01T00:00:00Z")]  # untriaged
    rd = data.RepoData("o/a", readable=True, issues=issues, prs=[])
    fr = data.FleetRepo("o/a", rd, lanes={})  # no lane deployed
    summary = data.repo_summary(fr)
    assert summary.unserviced is True
    assert summary.total_open == 1


def test_repo_summary_serviced_when_lane_deployed() -> None:
    issues = [_issue(1, "2026-01-01T00:00:00Z")]
    rd = data.RepoData("o/a", readable=True, issues=issues, prs=[])
    fr = data.FleetRepo("o/a", rd, lanes={"triage": status.LaneInfo(None, None)})
    assert data.repo_summary(fr).unserviced is False


# --- repo_detail / flow / callout --------------------------------------------


def test_repo_detail_unreadable() -> None:
    fr = data.FleetRepo("o/a", data.RepoData("o/a", readable=False), None)
    detail = data.repo_detail(fr, is_local=False, local_running=False)
    assert detail.readable is False
    assert detail.stages == []
    assert detail.callout is None


def test_repo_detail_stage_flow_and_pr_count() -> None:
    issues = [
        _issue(1, "2026-01-01T00:00:00Z", "agent-ready"),
        _issue(2, "2026-01-02T00:00:00Z", "backlog"),
    ]
    rd = data.RepoData("o/a", readable=True, issues=issues, prs=[_pr(5)])
    fr = data.FleetRepo("o/a", rd, lanes=None)
    detail = data.repo_detail(fr, is_local=False, local_running=False)
    by_key = {s.key: s for s in detail.stages}
    assert by_key["agent-ready"].count == 1
    assert by_key["backlog"].count == 1
    assert by_key["untriaged"].count == 0
    assert detail.pr_count == 1


def test_callout_ready_nothing_running_only_for_local_repo() -> None:
    issues = [_issue(1, "2026-01-01T00:00:00Z", "agent-ready")]
    rd = data.RepoData("o/a", readable=True, issues=issues, prs=[])
    fr = data.FleetRepo("o/a", rd, lanes=None)

    local = data.repo_detail(fr, is_local=True, local_running=False)
    assert local.callout == "1 ready, nothing running"

    # Same repo, but not the checkout the TUI was launched in — "nothing
    # running" cannot be known there, so the callout must stay blank.
    remote = data.repo_detail(fr, is_local=False, local_running=False)
    assert remote.callout is None


def test_callout_blank_when_something_is_already_running_locally() -> None:
    issues = [_issue(1, "2026-01-01T00:00:00Z", "agent-ready")]
    rd = data.RepoData("o/a", readable=True, issues=issues, prs=[])
    fr = data.FleetRepo("o/a", rd, lanes=None)
    detail = data.repo_detail(fr, is_local=True, local_running=True)
    assert detail.callout is None


def test_callout_untriaged_unserviced() -> None:
    issues = [_issue(1, "2026-01-01T00:00:00Z")]  # untriaged
    rd = data.RepoData("o/a", readable=True, issues=issues, prs=[])
    fr = data.FleetRepo("o/a", rd, lanes={})  # triage not deployed
    detail = data.repo_detail(fr, is_local=False, local_running=False)
    assert detail.callout == "1 untriaged, no lane deployed"


def test_callout_cron_scheduled_lane() -> None:
    issues = [_issue(1, "2026-01-01T00:00:00Z", "backlog")]
    rd = data.RepoData("o/a", readable=True, issues=issues, prs=[])
    fr = data.FleetRepo("o/a", rd, lanes={"groom": status.LaneInfo(None, "0 3 * * *")})
    detail = data.repo_detail(fr, is_local=False, local_running=False)
    assert detail.callout == "1 backlog, groom is cron-scheduled — nothing to do"


def test_callout_blank_when_lanes_unknown() -> None:
    issues = [_issue(1, "2026-01-01T00:00:00Z", "backlog")]
    rd = data.RepoData("o/a", readable=True, issues=issues, prs=[])
    fr = data.FleetRepo("o/a", rd, lanes=None)  # workflow listing unreadable
    detail = data.repo_detail(fr, is_local=False, local_running=False)
    assert detail.callout is None


# --- waiting_on_you ------------------------------------------------------------


def test_waiting_on_you_aggregates_fleet_and_local() -> None:
    issues = [
        _issue(1, "2026-01-01T00:00:00Z", "needs-human"),
        _issue(2, "2026-01-02T00:00:00Z"),  # untriaged, unserviced with lanes={}
    ]
    readable = data.FleetRepo(
        "o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[_pr(9)]), lanes={}
    )
    unreadable = data.FleetRepo("o/b", data.RepoData("o/b", readable=False), None)
    local = [runs.Run(1, "halted", "x"), runs.Run(2, "running", "y")]

    summary = data.waiting_on_you([readable, unreadable], local)
    assert summary.needs_human == 1
    assert summary.open_prs == 1
    assert summary.halted_runs == 1
    assert summary.unserviced_repos == 1
    assert summary.unreadable_repos == ("o/b",)


# --- issue_stage ---------------------------------------------------------------


def test_issue_stage_precedence() -> None:
    assert data.issue_stage(_issue(1, "x", "agent-ready", CLAIM_LABEL)) == CLAIM_LABEL
    assert data.issue_stage(_issue(1, "x", "backlog")) == "backlog"
    assert data.issue_stage(_issue(1, "x")) == "untriaged"


# --- command builders ------------------------------------------------------------


def test_dispatch_command() -> None:
    assert data.dispatch_command(42) == ["agent", "dispatch", "42", "--surface", "orca"]


def test_resume_command() -> None:
    assert data.resume_command(42) == ["agent", "resume", "42", "--surface", "orca"]


def test_open_web_command_pins_the_repo() -> None:
    # `--repo` is explicit rather than relying on cwd: the highlighted issue
    # may belong to a fleet repo the TUI wasn't launched in.
    assert data.open_web_command("o/a", 42) == [
        "gh",
        "issue",
        "view",
        "42",
        "--repo",
        "o/a",
        "--web",
    ]
