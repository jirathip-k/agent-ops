"""Smoke tests for the pipeline TUI (issue #232): boots, degrades on an
unreadable repo, and every action shells out only the command it displays.

Async Textual app tests are driven by hand with `asyncio.run` rather than a
pytest-asyncio-style plugin — the plan authorized exactly one new dependency
(`textual`) and no others.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Label

from agent_ops import github, registry, runs
from agent_ops.tui import app as tui_app
from agent_ops.tui import data
from agent_ops.tui.app import CommandBar, IssueList, ReposPane, RunsPane, StageRow, TuiApp


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
            assert "o/a" in str(items[0].query_one(Label).content)
            assert "loading" in str(items[0].query_one(Label).content)
            assert "o/b" in str(items[1].query_one(Label).content)

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
