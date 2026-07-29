"""Smoke tests for the pipeline TUI (issue #232): boots, degrades on an
unreadable repo, and every action shells out only the command it displays.

Async Textual app tests are driven by hand with `asyncio.run` rather than a
pytest-asyncio-style plugin — the plan authorized exactly one new dependency
(`textual`) and no others.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any, cast

import pytest
from textual.app import App, ComposeResult
from textual.content import Content
from textual.widgets import Label

from agent_ops import github, registry, runs, status, surfaces
from agent_ops.claims import CLAIM_LABEL
from agent_ops.tui import app as tui_app
from agent_ops.tui import data, sinks
from agent_ops.tui.app import (
    CommandBar,
    IssueDetailPane,
    IssueList,
    ReposPane,
    RunsPane,
    StageRow,
    TuiApp,
    WaitingPane,
)

# The real `load_issue_detail`, captured before the autouse `_no_real_gh`
# fixture below replaces `data.load_issue_detail` with a stub — tests that
# exercise the "PR listing failed/truncated" wiring end to end (#243) restore
# this and fake out `github.issue_view` instead, so the `prs_complete` flag
# actually threads through `_prs_for` → `load_issue_detail`.
_REAL_LOAD_ISSUE_DETAIL = data.load_issue_detail


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _issue(number: int, created: str, *labels: str) -> dict[str, Any]:
    return {"number": number, "createdAt": created, "labels": [{"name": n} for n in labels]}


def _set_fleet(monkeypatch: pytest.MonkeyPatch, fleet: list[data.FleetRepo]) -> None:
    """Point both the registry and `load_fleet` at the same fake fleet — the
    app now sizes its loading skeleton off the registry's repo list before
    `load_fleet` ever answers, so the two must agree on which repos exist."""
    monkeypatch.setattr(
        registry, "load_registry", lambda: registry.RegistryConfig(repos=[fr.repo for fr in fleet])
    )
    monkeypatch.setattr(data, "load_fleet", lambda config, **kwargs: fleet)


@pytest.fixture(autouse=True)
def _no_real_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may shell out for real — every test supplies its own fleet.

    Selecting an issue now fetches its detail (#235), so the default here
    must degrade rather than call `github.issue_view` for real; tests that
    care about the fetch itself override `data.load_issue_detail` locally.
    """
    monkeypatch.setattr(registry, "load_registry", lambda: registry.RegistryConfig(repos=[]))
    monkeypatch.setattr(data, "local_runs", lambda root: ([], True))
    monkeypatch.setattr(github, "remote_slug", lambda root: None)
    monkeypatch.setattr(
        data,
        "load_issue_detail",
        lambda repo, number, prs, **kwargs: data.IssueDetail(number, readable=False),
    )


def test_boots_with_empty_fleet(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.fleet == []
            assert app.query_one(ReposPane).rows == []
            # No crash rendering the command bar with nothing selected.
            app.query_one(CommandBar).set_preview(
                issue=None, repo=None, local=False, status_message=""
            )

    _run(scenario())


def test_degrades_when_registry_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom() -> registry.RegistryConfig:
        raise FileNotFoundError("no registry")

    monkeypatch.setattr(registry, "load_registry", boom)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.load_error is not None

    _run(scenario())


def test_unreadable_repo_shown_not_omitted(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fleet = [
        data.FleetRepo("o/good", data.RepoData("o/good", readable=True, issues=[], prs=[]), None),
        data.FleetRepo("o/bad", data.RepoData("o/bad", readable=False), None),
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            rows = app.query_one(ReposPane).rows
            assert all(row is not None for row in rows)
            names = [row.repo for row in rows if row is not None]
            assert names == ["o/good", "o/bad"]
            unreadable_row = rows[1]
            assert unreadable_row is not None
            assert unreadable_row.readable is False

    _run(scenario())


def test_selecting_a_repo_fills_flow_and_filters_issues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            assert stage_row.detail is not None
            assert stage_row.detail.repo == "o/a"
            # The only non-empty stage (agent-ready) is picked by default.
            assert stage_row.selected_stage is not None
            assert stage_row.selected_stage.key == "agent-ready"
            issue_list = app.query_one(IssueList)
            assert [i["number"] for i in issue_list.rows] == [7]

    _run(scenario())


def test_flow_header_shows_true_open_count_not_flow_stage_subtotal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#232's post-review feedback: `needs-human` and `triage:done` issues are
    real open issues but aren't `FLOW_ORDER` stages, so summing stage counts
    for the header undercounted and mislabeled the shortfall as the repo's
    total open issue count."""
    issues = [
        _issue(1, "2026-01-01T00:00:00Z", "agent-ready"),
        _issue(2, "2026-01-02T00:00:00Z", "needs-human"),
        _issue(3, "2026-01-03T00:00:00Z", "triage:done"),
    ]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            rendered = str(stage_row.content)
            assert "o/a — 3 open issue(s)" in rendered
            assert "1 open issue(s)" not in rendered

    _run(scenario())


def test_dispatch_runs_for_the_local_repos_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)

    calls: list[tuple[list[str], Path]] = []

    def fake_run(cmd: list[str], *, cwd: Path, check: bool, timeout: float) -> Any:
        calls.append((cmd, cwd))

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return Proc()

    monkeypatch.setattr(tui_app, "run_cmd", fake_run)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_dispatch()
            await app.workers.wait_for_complete()
            await pilot.pause()

        assert calls == [(data.dispatch_command(7), tmp_path)]

    _run(scenario())


def test_dispatch_is_refused_for_a_non_local_repo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/local-only")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo(
            "o/elsewhere", data.RepoData("o/elsewhere", readable=True, issues=issues, prs=[]), None
        ),
    ]
    _set_fleet(monkeypatch, fleet)

    def fail_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not shell out for a non-local repo's issue")

    monkeypatch.setattr(tui_app, "run_cmd", fail_run)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_dispatch()
            await pilot.pause()
            assert "not this checkout" in app.status_message

    _run(scenario())


def test_open_web_pins_repo_even_off_the_local_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/local-only")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo(
            "o/elsewhere", data.RepoData("o/elsewhere", readable=True, issues=issues, prs=[]), None
        ),
    ]
    _set_fleet(monkeypatch, fleet)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, cwd: Path, check: bool, timeout: float) -> Any:
        calls.append(cmd)

        class Proc:
            returncode = 0
            stdout = ""
            stderr = ""

        return Proc()

    monkeypatch.setattr(tui_app, "run_cmd", fake_run)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_open_web()
            await app.workers.wait_for_complete()
            await pilot.pause()

        assert calls == [data.open_web_command("o/elsewhere", 7)]

    _run(scenario())


def test_dispatch_failure_is_reported_not_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)

    def failing_run(cmd: list[str], *, cwd: Path, check: bool, timeout: float) -> Any:
        class Proc:
            returncode = 1
            stdout = ""
            stderr = "no worktree there"

        return Proc()

    monkeypatch.setattr(tui_app, "run_cmd", failing_run)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_dispatch()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "no worktree there" in app.status_message

    _run(scenario())


# --- `t`: trigger the lane servicing the focused stage (#253) -----------------
#
# `StageRow`'s own `t` binding only fires while it has focus (mirroring how
# its left/right bindings only move the stage cursor while focused) — so most
# of these tests explicitly `.focus()` it first, the same way
# `test_focused_pane_border_is_distinguishable_from_an_unfocused_sibling`
# above drives focus with `pilot.press`.


def _lane_fleet(
    repo: str, issues: list[dict[str, Any]], **lanes: status.LaneInfo
) -> data.FleetRepo:
    return data.FleetRepo(repo, data.RepoData(repo, readable=True, issues=issues, prs=[]), lanes)


class _ProcStub:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_trigger_key_is_a_noop_when_stage_row_is_not_focused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [_lane_fleet("o/a", issues, triage=status.LaneInfo(None, None, "triage.yml"))]
    _set_fleet(monkeypatch, fleet)

    def fail_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not shell out — the binding shouldn't even fire")

    monkeypatch.setattr(tui_app, "run_cmd", fail_run)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            # ReposPane holds focus by default (on_mount) — not StageRow.
            assert app.query_one(ReposPane).has_focus
            await pilot.press("t")
            await pilot.pause()
            assert app._pending_trigger is None
            assert "trigger" not in app.status_message.lower()

    _run(scenario())


def test_trigger_no_stage_selected_does_not_crash(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            stage_row.focus()
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert "no stage selected" in app.status_message

    _run(scenario())


def test_trigger_unserviced_stage_is_a_noop_with_explanation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(1, "2026-01-01T00:00:00Z")]  # untriaged, no lane deployed
    fleet = [data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), {})]
    _set_fleet(monkeypatch, fleet)

    def fail_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not shell out for an unserviced stage")

    monkeypatch.setattr(tui_app, "run_cmd", fail_run)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            stage_row.focus()
            await pilot.pause()
            assert stage_row.selected_stage is not None
            assert stage_row.selected_stage.key == "untriaged"
            await pilot.press("t")
            await pilot.pause()
            assert app._pending_trigger is None
            assert "unserviced" in app.status_message

    _run(scenario())


def test_trigger_empty_stage_is_a_noop_with_explanation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    fleet = [_lane_fleet("o/a", [], triage=status.LaneInfo(None, None, "triage.yml"))]
    _set_fleet(monkeypatch, fleet)

    def fail_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not shell out for an empty stage")

    monkeypatch.setattr(tui_app, "run_cmd", fail_run)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            stage_row.focus()
            await pilot.pause()
            assert stage_row.selected_stage is not None
            assert stage_row.selected_stage.count == 0
            await pilot.press("t")
            await pilot.pause()
            assert app._pending_trigger is None
            assert "empty" in app.status_message

    _run(scenario())


def test_trigger_run_stage_is_a_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(1, "2026-01-01T00:00:00Z", CLAIM_LABEL)]
    fleet = [_lane_fleet("o/a", issues, triage=status.LaneInfo(None, None, "triage.yml"))]
    _set_fleet(monkeypatch, fleet)

    def fail_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not shell out for the run (agent:claimed) stage")

    monkeypatch.setattr(tui_app, "run_cmd", fail_run)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            stage_row.focus()
            await pilot.pause()
            assert stage_row.selected_stage is not None
            assert stage_row.selected_stage.key == CLAIM_LABEL
            await pilot.press("t")
            await pilot.pause()
            assert app._pending_trigger is None

    _run(scenario())


def test_trigger_agent_ready_local_choice_runs_exactly_dispatch_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [_lane_fleet("o/a", issues, triage=status.LaneInfo(None, None, "triage.yml"))]
    _set_fleet(monkeypatch, fleet)

    calls: list[tuple[list[str], Path]] = []

    def fake_run(cmd: list[str], *, cwd: Path, check: bool, timeout: float) -> Any:
        calls.append((cmd, cwd))
        return _ProcStub()

    monkeypatch.setattr(tui_app, "run_cmd", fake_run)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            stage_row.focus()
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert app._pending_trigger is not None
            assert app._pending_trigger.phase == "destination"
            await pilot.press("1")  # local
            await app.workers.wait_for_complete()
            await pilot.pause()

        assert calls == [(data.dispatch_command(7), tmp_path)]

    _run(scenario())


def test_trigger_cloud_choice_reports_queued_with_run_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [_lane_fleet("o/a", issues, triage=status.LaneInfo(None, None, "triage.yml"))]
    _set_fleet(monkeypatch, fleet)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, cwd: Path, check: bool, timeout: float) -> Any:
        calls.append(cmd)
        if cmd[:3] == ["gh", "run", "list"]:
            rows = [
                {
                    "url": "https://github.com/o/a/actions/runs/123",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "event": "workflow_dispatch",
                }
            ]
            return _ProcStub(stdout=json.dumps(rows))
        return _ProcStub()

    monkeypatch.setattr(tui_app, "run_cmd", fake_run)
    monkeypatch.setattr(tui_app.time, "sleep", lambda s: None)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            stage_row.focus()
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            await pilot.press("2")  # cloud
            await app.workers.wait_for_complete()
            await pilot.pause()

        assert calls[0] == ["gh", "workflow", "run", "triage.yml", "--repo", "o/a"]
        assert "queued" in app.status_message
        assert "done" not in app.status_message
        assert "https://github.com/o/a/actions/runs/123" in app.status_message

    _run(scenario())


def test_trigger_cloud_choice_falls_back_when_run_is_not_yet_visible(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [_lane_fleet("o/a", issues, triage=status.LaneInfo(None, None, "triage.yml"))]
    _set_fleet(monkeypatch, fleet)

    def fake_run(cmd: list[str], *, cwd: Path, check: bool, timeout: float) -> Any:
        if cmd[:3] == ["gh", "run", "list"]:
            return _ProcStub(stdout="[]")
        return _ProcStub()

    monkeypatch.setattr(tui_app, "run_cmd", fake_run)
    monkeypatch.setattr(tui_app.time, "sleep", lambda s: None)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            stage_row.focus()
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            await pilot.press("2")  # cloud
            await app.workers.wait_for_complete()
            await pilot.pause()

        assert "queued" in app.status_message
        assert "gh run list" in app.status_message

    _run(scenario())


def test_trigger_non_local_repo_only_offers_cloud_and_runs_it_directly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/local-only")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [_lane_fleet("o/elsewhere", issues, triage=status.LaneInfo(None, None, "triage.yml"))]
    _set_fleet(monkeypatch, fleet)

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], *, cwd: Path, check: bool, timeout: float) -> Any:
        calls.append(cmd)
        return _ProcStub(stdout="[]")

    monkeypatch.setattr(tui_app, "run_cmd", fake_run)
    monkeypatch.setattr(tui_app.time, "sleep", lambda s: None)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            stage_row.focus()
            await pilot.pause()
            await pilot.press("t")
            await app.workers.wait_for_complete()
            await pilot.pause()

        # Not the local checkout — cloud is the only viable destination, so
        # there is nothing to ask; it must run without a destination prompt.
        assert app._pending_trigger is None
        assert calls[0] == ["gh", "workflow", "run", "triage.yml", "--repo", "o/elsewhere"]
        assert "queued" in app.status_message

    _run(scenario())


def test_trigger_untriaged_local_choice_streams_via_surfaces_not_run_cmd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """untriaged/backlog/spec-requested run `agent <lane>` start-to-finish (no
    `--surface`), unlike the agent-ready/plan-requested pairings — routing
    these through the blocking `run_cmd` `_exec` uses would tie up a worker
    thread for the whole run with no live output, so they must go through
    `surfaces.pick(...).spawn(...)` directly instead (#253)."""
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(1, "2026-01-01T00:00:00Z")]  # untriaged
    fleet = [_lane_fleet("o/a", issues, triage=status.LaneInfo(None, None, "triage.yml"))]
    _set_fleet(monkeypatch, fleet)

    def fail_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not use the blocking run_cmd path for a streamed local lane")

    monkeypatch.setattr(tui_app, "run_cmd", fail_run)

    spawn_calls: list[tuple[str, list[str], Path]] = []

    class _FakeSurface:
        def spawn(
            self,
            label: str,
            command: list[str],
            cwd: Path,
            attach_path: Path | None = None,
        ) -> surfaces.Spawned:
            spawn_calls.append((label, command, cwd))
            return surfaces.Spawned(where="background pid 123", surface="background", pid=123)

    monkeypatch.setattr(surfaces, "pick", lambda name="auto": _FakeSurface())

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            stage_row.focus()
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert app._pending_trigger is not None
            await pilot.press("1")  # local
            await app.workers.wait_for_complete()
            await pilot.pause()

        assert spawn_calls == [("agent-triage", ["agent", "triage"], tmp_path)]

    _run(scenario())


def test_trigger_multi_consumer_stage_prompts_for_lane_and_cancel_spawns_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(status, "STAGE_CONSUMERS", {"backlog": frozenset({"groom", "triage"})})
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(3, "2026-01-01T00:00:00Z", "backlog")]
    fleet = [
        _lane_fleet(
            "o/a",
            issues,
            groom=status.LaneInfo(None, None, "groom.yml"),
            triage=status.LaneInfo(None, None, "triage.yml"),
        )
    ]
    _set_fleet(monkeypatch, fleet)

    def fail_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("must not shell out before a lane is chosen")

    monkeypatch.setattr(tui_app, "run_cmd", fail_run)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            stage_row.focus()
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            assert app._pending_trigger is not None
            assert app._pending_trigger.phase == "lane"
            assert app._pending_trigger.lanes == ("groom", "triage")

            await pilot.press("escape")
            await pilot.pause()
            assert app._pending_trigger is None
            assert "cancelled" in app.status_message

    _run(scenario())


def test_trigger_multi_consumer_stage_choosing_a_lane_moves_to_destination_choice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(status, "STAGE_CONSUMERS", {"backlog": frozenset({"groom", "triage"})})
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(3, "2026-01-01T00:00:00Z", "backlog")]
    fleet = [
        _lane_fleet(
            "o/a",
            issues,
            groom=status.LaneInfo(None, None, "groom.yml"),
            triage=status.LaneInfo(None, None, "triage.yml"),
        )
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            stage_row.focus()
            await pilot.pause()
            await pilot.press("t")
            await pilot.pause()
            await pilot.press("1")  # first lane, sorted: "groom"
            await pilot.pause()
            assert app._pending_trigger is not None
            assert app._pending_trigger.phase == "destination"
            assert app._pending_trigger.lane == "groom"

    _run(scenario())


def test_narrow_layout_toggles_below_the_threshold(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            body = app.query_one("#body")
            assert not body.has_class("narrow")

            await pilot.resize_terminal(60, 24)
            await pilot.pause()
            assert body.has_class("narrow")

    _run(scenario())


def test_runs_pane_reflects_local_runs_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = [runs.Run(3, "halted", "self-review — resume with `agent resume 3`")]
    monkeypatch.setattr(data, "local_runs", lambda root: (local, True))

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one(RunsPane).rows == local

    _run(scenario())


def test_action_with_nothing_selected_does_not_crash(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_dispatch()
            app.action_resume()
            app.action_open_web()
            await pilot.pause()
            assert "no issue selected" in app.status_message

    _run(scenario())


# --- incremental fleet loading (#232's post-review fix) -----------------------
#
# `agent status --pipeline` fetching seven repos one at a time measured at
# 47s; the TUI sat on a bare "loading…" for the whole sweep. The fix moved the
# concurrency into `status.fetch_repos` and had the TUI paint each repo's row
# the moment its own fetch lands, rather than waiting for the slowest repo in
# the fleet.


class _ReposHarness(App[None]):
    """A bare host for exercising `ReposPane` without the rest of `TuiApp`."""

    def compose(self) -> ComposeResult:
        yield ReposPane(id="repos")


def test_repos_pane_update_row_leaves_other_rows_and_selection_alone() -> None:
    async def scenario() -> None:
        app = _ReposHarness()
        async with app.run_test(size=(80, 24)) as pilot:
            pane = app.query_one(ReposPane)
            pane.show_loading(["o/a", "o/b", "o/c"])
            await pilot.pause()
            pane.index = 1  # the user has navigated to the second row
            await pilot.pause()

            pane.update_row(2, data.RepoSummary("o/c", True, 5, False, False))
            await pilot.pause()

            # An unrelated row landing must not disturb the user's selection —
            # a full clear()+rebuild resets ListView.index, which a naive
            # incremental implementation would do on every arrival.
            assert pane.index == 1
            assert pane.rows[0] is None  # o/a still outstanding
            assert pane.rows[1] is None  # o/b still outstanding
            assert pane.rows[2] is not None and pane.rows[2].total_open == 5

            items = list(pane.children)
            assert "loading" in str(items[0].query_one(Label).content)
            assert "loading" in str(items[1].query_one(Label).content)
            assert "5" in str(items[2].query_one(Label).content)

    _run(scenario())


def test_repos_pane_show_loading_lists_every_repo_as_outstanding() -> None:
    async def scenario() -> None:
        app = _ReposHarness()
        async with app.run_test(size=(80, 24)) as pilot:
            pane = app.query_one(ReposPane)
            pane.show_loading(["o/a", "o/b"])
            await pilot.pause()

            assert pane.rows == [None, None]
            items = list(pane.children)
            # Bare short name, not the "o/" prefix — each is unique on its own.
            assert "a" in str(items[0].query_one(Label).content)
            assert "loading" in str(items[0].query_one(Label).content)
            assert "b" in str(items[1].query_one(Label).content)

    _run(scenario())


def test_repos_pane_elides_shared_owner_but_keeps_colliding_names() -> None:
    """The truncation bug from #232's post-review feedback: with a long owner
    shared by most rows, `owner/repo` in full left three of four rows
    indistinguishable in a narrow pane. Rows whose short name is unique drop
    the owner; a short name shared by two different owners keeps it, since
    that's the only thing still telling those two apart."""

    async def scenario() -> None:
        app = _ReposHarness()
        async with app.run_test(size=(40, 24)) as pilot:
            pane = app.query_one(ReposPane)
            pane.show_loading(
                [
                    "synergy-services-cooling-tower/synergy-costing",
                    "synergy-services-cooling-tower/synergy-erp",
                    "jirathip-k/agent-ops",
                    "alice/widgets",
                    "bob/widgets",
                ]
            )
            await pilot.pause()

            items = list(pane.children)
            assert str(items[0].query_one(Label).content).startswith("synergy-costing")
            assert str(items[1].query_one(Label).content).startswith("synergy-erp")
            assert str(items[2].query_one(Label).content).startswith("agent-ops")
            assert str(items[3].query_one(Label).content).startswith("alice/widgets")
            assert str(items[4].query_one(Label).content).startswith("bob/widgets")

    _run(scenario())


def test_app_paints_rows_as_each_repos_own_fetch_completes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    release = threading.Event()
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=[], prs=[]), None),
        data.FleetRepo("o/b", data.RepoData("o/b", readable=True, issues=[], prs=[]), None),
    ]

    def fake_load_fleet(
        config: registry.RegistryConfig,
        *,
        on_repo: Any = None,
    ) -> list[data.FleetRepo]:
        # o/a (index 0) answers immediately; o/b (index 1) waits for the test
        # to release it — simulating the concurrent sweep's out-of-order
        # arrival without depending on real thread timing.
        if on_repo is not None:
            on_repo(0, fleet[0])
        release.wait(timeout=5)
        if on_repo is not None:
            on_repo(1, fleet[1])
        return fleet

    monkeypatch.setattr(
        registry, "load_registry", lambda: registry.RegistryConfig(repos=["o/a", "o/b"])
    )
    monkeypatch.setattr(data, "load_fleet", fake_load_fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            # Poll until o/a's row is painted while o/b is still outstanding —
            # the whole point of rendering incrementally rather than blocking
            # on the full sweep.
            for _ in range(200):
                if app.query_one(ReposPane).rows[0] is not None:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)

            rows = app.query_one(ReposPane).rows
            assert rows[0] is not None and rows[0].repo == "o/a"
            assert rows[1] is None  # o/b hasn't landed yet

            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()

            rows = app.query_one(ReposPane).rows
            assert rows[1] is not None and rows[1].repo == "o/b"

    _run(scenario())


# --- issue detail pane: fetched lazily on selection, cached per issue (#235) --


def test_selecting_an_issue_shows_detail_and_fetches_only_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)

    calls: list[tuple[str, int]] = []
    detail = data.IssueDetail(
        7,
        readable=True,
        title="Fix it",
        body="do the thing",
        labels=("agent-ready",),
        age="3d",
        has_open_pr=False,
    )

    def fake_load_issue_detail(
        repo: str, number: int, prs: list[Any], **kwargs: Any
    ) -> data.IssueDetail:
        calls.append((repo, number))
        return detail

    monkeypatch.setattr(data, "load_issue_detail", fake_load_issue_detail)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            pane = app.query_one(IssueDetailPane)
            rendered = str(pane._body.content)
            assert "Fix it" in rendered
            assert "do the thing" in rendered
            assert "agent-ready" in rendered
            assert "3d" in rendered
            assert "open PR: no" in rendered
            assert calls == [("o/a", 7)]

            # Re-showing the same selection (e.g. a second Highlighted event
            # for the same row) must hit the cache, not fetch again.
            app._show_issue_detail()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert calls == [("o/a", 7)]

    _run(scenario())


# --- PR listing failed/truncated must render "unknown", never "no" (#243) ---
#
# These restore the real `data.load_issue_detail` (the autouse fixture stubs
# it) and fake `github.issue_view` instead, so `_prs_for`'s completeness flag
# actually threads through `_show_issue_detail` → `load_issue_detail` end to
# end, the same path a real TUI session runs.


def test_unreadable_pr_listing_shows_open_pr_as_unknown_not_no(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    monkeypatch.setattr(data, "load_issue_detail", _REAL_LOAD_ISSUE_DETAIL)
    raw = {
        "number": 7,
        "title": "Fix it",
        "body": "the body",
        "labels": [{"name": "agent-ready"}],
        "createdAt": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(github, "issue_view", lambda repo, number: raw)
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo(
            "o/a",
            data.RepoData("o/a", readable=True, issues=issues, prs=[], prs_readable=False),
            None,
        ),
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            pane = app.query_one(IssueDetailPane)
            rendered = str(pane._body.content)
            assert "open PR: ?" in rendered
            assert "open PR: no" not in rendered

    _run(scenario())


def test_truncated_pr_listing_with_no_match_shows_open_pr_as_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    monkeypatch.setattr(data, "load_issue_detail", _REAL_LOAD_ISSUE_DETAIL)
    raw = {
        "number": 7,
        "title": "Fix it",
        "body": "the body",
        "labels": [{"name": "agent-ready"}],
        "createdAt": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(github, "issue_view", lambda repo, number: raw)
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    # None of these PRs reference #7 — a truncated listing must not let that
    # non-match be read as "no open PR".
    prs = [
        {"number": n, "headRefName": f"fix/issue-{100 + n}", "closingIssuesReferences": []}
        for n in range(status.OPEN_PR_LIMIT)
    ]
    fleet = [
        data.FleetRepo(
            "o/a",
            data.RepoData("o/a", readable=True, issues=issues, prs=prs, prs_truncated=True),
            None,
        ),
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            pane = app.query_one(IssueDetailPane)
            assert "open PR: ?" in str(pane._body.content)
            stage_row = app.query_one(StageRow)
            assert f"PRs open: ≥{status.OPEN_PR_LIMIT}" in str(stage_row.content)

    _run(scenario())


def test_truncated_pr_listing_with_a_match_still_shows_yes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A truncated listing is still usable when it *does* contain a match —
    only the negative case ("not found") is untrustworthy."""
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    monkeypatch.setattr(data, "load_issue_detail", _REAL_LOAD_ISSUE_DETAIL)
    raw = {
        "number": 7,
        "title": "Fix it",
        "body": "the body",
        "labels": [{"name": "agent-ready"}],
        "createdAt": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(github, "issue_view", lambda repo, number: raw)
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    prs = [{"number": 999, "headRefName": "fix/issue-7", "closingIssuesReferences": []}]
    fleet = [
        data.FleetRepo(
            "o/a",
            data.RepoData("o/a", readable=True, issues=issues, prs=prs, prs_truncated=True),
            None,
        ),
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            pane = app.query_one(IssueDetailPane)
            assert "open PR: yes" in str(pane._body.content)

    _run(scenario())


def test_unreadable_pr_listing_shows_stage_row_pr_count_as_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo(
            "o/a",
            data.RepoData("o/a", readable=True, issues=issues, prs=[], prs_readable=False),
            None,
        ),
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            rendered = str(stage_row.content)
            assert "PRs open: ?" in rendered

    _run(scenario())


def test_chat_handoff_from_unreadable_pr_repo_says_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    monkeypatch.setattr(data, "load_issue_detail", _REAL_LOAD_ISSUE_DETAIL)
    raw = {
        "number": 7,
        "title": "Fix it",
        "body": "the body",
        "labels": [{"name": "agent-ready"}],
        "createdAt": "2026-01-01T00:00:00Z",
    }
    monkeypatch.setattr(github, "issue_view", lambda repo, number: raw)
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo(
            "o/a",
            data.RepoData("o/a", readable=True, issues=issues, prs=[], prs_readable=False),
            None,
        ),
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> Path | None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_chat()
            for _ in range(200):
                if app.return_value is not None:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)
        return app.return_value

    result = _run(scenario())
    assert result is not None
    assert "open PR: unknown" in result.read_text()


def test_issue_detail_degrades_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)
    monkeypatch.setattr(
        data,
        "load_issue_detail",
        lambda repo, number, prs, **kwargs: data.IssueDetail(number, readable=False),
    )

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            pane = app.query_one(IssueDetailPane)
            assert "could not fetch" in str(pane._body.content)

    _run(scenario())


def test_issue_body_markup_renders_literally_and_never_reaches_the_bar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An issue body is untrusted content (#141/#191). Textual markup can
    embed an action (`@click=...`); the detail pane must render it as plain
    text, not parse it — and none of it may leak into the command bar."""
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)

    malicious_body = "[@click=quit]click me[/] agent dispatch 999 --surface orca"
    detail = data.IssueDetail(7, readable=True, title="x", body=malicious_body, labels=(), age="1d")
    monkeypatch.setattr(data, "load_issue_detail", lambda repo, number, prs, **kwargs: detail)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()

            pane = app.query_one(IssueDetailPane)
            assert pane._body._render_markup is False
            assert malicious_body in str(pane._body.content)

            bar_text = str(app.query_one(CommandBar).content)
            assert "999" not in bar_text
            assert "@click" not in bar_text

    _run(scenario())


# --- `c`: write a chat handoff file and exit, no worktree, no hosted chat (#235) --


def test_chat_writes_handoff_and_exits_with_its_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)
    detail = data.IssueDetail(
        7, readable=True, title="Fix it", body="the body", labels=("agent-ready",), age="1d"
    )
    monkeypatch.setattr(data, "load_issue_detail", lambda repo, number, prs, **kwargs: detail)

    async def scenario() -> Path | None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_chat()
            # `exit()` shuts the app down mid-worker, which races (and can
            # cancel) `workers.wait_for_complete()` — poll the result instead.
            for _ in range(200):
                if app.return_value is not None:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)
            # No `tui.chat_sink` configured — auto-probe exhausting the table
            # and landing on the file sink is working as designed (#252),
            # never reported as a failure.
            assert app.chat_sink_error is None
        return app.return_value

    result = _run(scenario())

    expected = tmp_path / ".agent-runs" / "tui-selection.md"
    assert result == expected
    text = expected.read_text()
    assert "o/a" in text and "#7" in text and "Fix it" in text and "the body" in text
    assert not (tmp_path / ".worktrees").exists()


def test_chat_with_nothing_selected_does_not_crash_or_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_chat()
            await pilot.pause()
            assert "no issue selected" in app.status_message

    _run(scenario())
    assert not (tmp_path / ".agent-runs" / "tui-selection.md").exists()


class _StubLiveSink:
    """A `ChatSink` (#249) that always delivers — stands in for tmux/Orca/a
    `tui.chat_sink` command without shelling out."""

    name = "tmux"

    def available(self) -> bool:
        return True

    def send(self, text: str) -> bool:
        return True


def test_chat_through_a_live_sink_stays_running_and_names_the_sink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#249: when a multiplexer/Orca/command sink actually delivers the
    handoff, the TUI does not exit — the multiplexer already split the
    screen, so there's nothing left for `c` to hand off by quitting."""
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)
    detail = data.IssueDetail(
        7, readable=True, title="Fix it", body="the body", labels=("agent-ready",), age="1d"
    )
    monkeypatch.setattr(data, "load_issue_detail", lambda repo, number, prs, **kwargs: detail)
    monkeypatch.setattr(
        sinks, "deliver", lambda text, config_value, env, project_root: (_StubLiveSink(), None)
    )

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_chat()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.return_value is None
            assert "tmux" in app.status_message
            assert app.chat_sink_error is None

    _run(scenario())
    assert not (tmp_path / ".agent-runs" / "tui-selection.md").exists()


def test_chat_reports_when_a_configured_sink_fails_and_falls_back_to_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#252: a configured `tui.chat_sink` that fails must set
    `chat_sink_error` so the CLI layer can report it — the handoff path
    returned via `exit()`/`return_value` is unaffected; the message travels
    through the separate `chat_sink_error` field instead."""
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)
    detail = data.IssueDetail(
        7, readable=True, title="Fix it", body="the body", labels=("agent-ready",), age="1d"
    )
    monkeypatch.setattr(data, "load_issue_detail", lambda repo, number, prs, **kwargs: detail)
    file_sink = sinks.FileSink(tmp_path / ".agent-runs" / "tui-selection.md")
    error_message = 'chat_sink "mymux send -- {text}" failed (exit 127)'
    monkeypatch.setattr(
        sinks,
        "deliver",
        lambda text, config_value, env, project_root: (file_sink, error_message),
    )

    async def scenario() -> Path | None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.action_chat()
            for _ in range(200):
                if app.return_value is not None:
                    break
                await pilot.pause()
                await asyncio.sleep(0.01)
            assert app.chat_sink_error == error_message
        return app.return_value

    result = _run(scenario())
    assert result == tmp_path / ".agent-runs" / "tui-selection.md"


def test_narrow_layout_still_works_with_the_detail_pane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(60, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.query_one("#body").has_class("narrow")
            # The detail pane mounts and renders without crashing the layout.
            assert app.query_one(IssueDetailPane) is not None

    _run(scenario())


# --- theming (issue #248): Catppuccin default, configurable, focus/colour ------


def test_theme_defaults_to_catppuccin_macchiato(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)):
            await app.workers.wait_for_complete()
            assert app.theme == "catppuccin-macchiato"

    _run(scenario())


def test_theme_kwarg_selects_a_different_built_in_theme(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = TuiApp(tmp_path, theme="nord")
        async with app.run_test(size=(80, 24)):
            await app.workers.wait_for_complete()
            assert app.theme == "nord"

    _run(scenario())


def test_focused_pane_border_is_distinguishable_from_an_unfocused_sibling(
    tmp_path: Path,
) -> None:
    """The double/round edge is the non-colour channel (#248): it must differ
    even with colour stripped, so this asserts on the border edge type, not
    just the border colour."""

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            repos_pane = app.query_one(ReposPane)
            runs_pane = app.query_one(RunsPane)
            assert repos_pane.has_focus
            assert not runs_pane.has_focus
            focused_edge, _ = repos_pane.styles.border_top
            unfocused_edge, _ = runs_pane.styles.border_top
            assert focused_edge != unfocused_edge

            await pilot.press("tab")
            await pilot.pause()
            assert not repos_pane.has_focus
            new_edge, _ = repos_pane.styles.border_top
            assert new_edge == unfocused_edge

    _run(scenario())


def test_waiting_pane_colours_nonzero_counts_but_keeps_the_text_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(1, "2026-01-01T00:00:00Z", "needs-human")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)):
            await app.workers.wait_for_complete()
            pane = app.query_one(WaitingPane)
            rendered = cast(Content, pane.render())
            text = str(rendered)
            # The colour-stripped channel: the count and label stay legible
            # on their own.
            assert "1  needs-human" in text
            assert any(span.style == "$warning" for span in rendered.spans)

    _run(scenario())


def test_waiting_pane_colours_zero_counts_as_success_when_all_clear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=[], prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)):
            await app.workers.wait_for_complete()
            pane = app.query_one(WaitingPane)
            rendered = cast(Content, pane.render())
            text = str(rendered)
            assert "0  needs-human" in text
            assert any(span.style == "$success" for span in rendered.spans)
            assert not any(span.style == "$error" for span in rendered.spans)

    _run(scenario())


def test_stage_row_colours_an_unserviced_stage_but_keeps_the_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(1, "2026-01-01T00:00:00Z")]  # untriaged
    rd = data.RepoData("o/a", readable=True, issues=issues, prs=[])
    fleet = [data.FleetRepo("o/a", rd, lanes={})]  # no lane deployed → unserviced
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            stage_row = app.query_one(StageRow)
            rendered = cast(Content, stage_row.render())
            text = str(rendered)
            # Colour-stripped channel: the ⚠ marker on the untriaged column.
            assert "untriaged:1" in text
            assert "⚠" in text
            assert any(span.style == "$error" for span in rendered.spans)

    _run(scenario())


def test_issue_list_marks_needs_human_labelled_issues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready", "needs-human")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    _set_fleet(monkeypatch, fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)):
            await app.workers.wait_for_complete()
            issue_list = app.query_one(IssueList)
            label = issue_list.query_one(Label)
            rendered = cast(Content, label.render())
            text = str(rendered)
            assert "#7" in text
            assert "needs-human" in text
            assert any(span.style == "$warning" for span in rendered.spans)

    _run(scenario())
