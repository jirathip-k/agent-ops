"""Smoke tests for the pipeline TUI (issue #232): boots, degrades on an
unreadable repo, and every action shells out only the command it displays.

Async Textual app tests are driven by hand with `asyncio.run` rather than a
pytest-asyncio-style plugin — the plan authorized exactly one new dependency
(`textual`) and no others.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agent_ops import github, registry, runs
from agent_ops.tui import app as tui_app
from agent_ops.tui import data
from agent_ops.tui.app import CommandBar, IssueList, ReposPane, RunsPane, StageRow, TuiApp


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _issue(number: int, created: str, *labels: str) -> dict[str, Any]:
    return {"number": number, "createdAt": created, "labels": [{"name": n} for n in labels]}


@pytest.fixture(autouse=True)
def _no_real_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing here may shell out for real — every test supplies its own fleet."""
    monkeypatch.setattr(registry, "load_registry", lambda: registry.RegistryConfig(repos=[]))
    monkeypatch.setattr(data, "local_runs", lambda root: ([], True))
    monkeypatch.setattr(github, "remote_slug", lambda root: None)


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
    monkeypatch.setattr(data, "load_fleet", lambda config: fleet)

    async def scenario() -> None:
        app = TuiApp(tmp_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            names = [row.repo for row in app.query_one(ReposPane).rows]
            assert names == ["o/good", "o/bad"]
            unreadable_row = app.query_one(ReposPane).rows[1]
            assert unreadable_row.readable is False

    _run(scenario())


def test_selecting_a_repo_fills_flow_and_filters_issues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    monkeypatch.setattr(data, "load_fleet", lambda config: fleet)

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


def test_dispatch_runs_for_the_local_repos_issue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda root: "o/a")
    issues = [_issue(7, "2026-01-01T00:00:00Z", "agent-ready")]
    fleet = [
        data.FleetRepo("o/a", data.RepoData("o/a", readable=True, issues=issues, prs=[]), None),
    ]
    monkeypatch.setattr(data, "load_fleet", lambda config: fleet)

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
    monkeypatch.setattr(data, "load_fleet", lambda config: fleet)

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
    monkeypatch.setattr(data, "load_fleet", lambda config: fleet)

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
    monkeypatch.setattr(data, "load_fleet", lambda config: fleet)

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
