"""The pipeline TUI (issue #232): one screen for status/runs/dispatch/resume,
with every action showing the command it is about to run.

Read-only by default; `d`/`r`/`o` are the only actions, and each shells out to
the real CLI command it displays — never a re-implementation of dispatch or
resume logic. No new state: everything comes from `agent_ops.tui.data`, which
itself is a thin wrapper over `status.py`/`runs.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Any

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical
from textual.message import Message
from textual.widgets import Label, ListItem, ListView, Static

from agent_ops import github, registry, runs, status
from agent_ops.tui import data
from agent_ops.utils import run as run_cmd

# Below this width the 2x2 grid no longer has room to stay side-by-side —
# panes stack instead, per the narrow-terminal layout in #232's design. Set
# under 80 so the ordinary 80-column terminal (#232's stated baseline) still
# gets the wide grid; something narrower (a phone over SSH) gets the stack.
NARROW_WIDTH = 76


def _issue_line(issue: dict[str, Any]) -> str:
    number = issue.get("number", "?")
    age = status._age(issue["createdAt"]) if issue.get("createdAt") else "?"
    return f"#{number}  {age}"


def _select_first(list_view: ListView) -> None:
    """Highlight row 0 as soon as a list has real content.

    `ListView.index` otherwise stays `None` until the user presses an arrow
    key inside it (Textual's own default), which would leave the command bar
    reading "(select an issue)" for a repo with exactly one dispatchable
    issue — the opposite of "the command is visible."
    """
    list_view.index = 0


class ReposPane(ListView):
    """Fleet repo list — always-visible ⚠ marker for an unserviced stage (#229)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rows: list[data.RepoSummary] = []

    def on_mount(self) -> None:
        self.border_title = "Repos"

    def set_rows(self, rows: list[data.RepoSummary]) -> None:
        self.rows = rows
        self.clear()
        if not rows:
            self.append(ListItem(Label("(no repos registered)")))
            return
        for row in rows:
            self.append(ListItem(Label(self._line(row))))
        _select_first(self)

    @staticmethod
    def _line(row: data.RepoSummary) -> str:
        if not row.readable:
            return f"{row.repo}  ⚠ unreadable"
        count = f"≥{row.total_open}" if row.truncated else str(row.total_open)
        marker = " ⚠" if row.unserviced else ""
        return f"{row.repo}  {count}{marker}"


class StageRow(Static):
    """The flow line — focusable; left/right pick a stage to filter issues by."""

    can_focus = True
    BINDINGS = [
        Binding("left", "prev_stage", "◀ stage", show=False),
        Binding("right", "next_stage", "▶ stage", show=False),
    ]

    class StageChanged(Message):
        """Posted when left/right pick a different stage to filter issues by."""

        def __init__(self, stage_index: int) -> None:
            super().__init__()
            self.stage_index = stage_index

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.detail: data.RepoDetail | None = None
        self.stage_index = 0

    def on_mount(self) -> None:
        self.border_title = "flow"

    def action_prev_stage(self) -> None:
        self._move(-1)

    def action_next_stage(self) -> None:
        self._move(1)

    def _move(self, delta: int) -> None:
        if not self.detail or not self.detail.stages:
            return
        self.stage_index = (self.stage_index + delta) % len(self.detail.stages)
        self.render_detail()
        self.post_message(self.StageChanged(self.stage_index))

    def set_detail(self, detail: data.RepoDetail, *, reset_stage: bool) -> None:
        self.detail = detail
        if reset_stage or not detail.stages:
            self.stage_index = next((i for i, s in enumerate(detail.stages) if s.count), 0)
        self.render_detail()

    def render_detail(self) -> None:
        d = self.detail
        if d is None:
            self.update("(no repo selected)")
            return
        if not d.readable:
            self.update(f"{d.repo}\n⚠ unreadable — could not list this repo's issues")
            return
        total = sum(s.count for s in d.stages)
        header = f"{d.repo} — {total} open issue(s)"
        if d.truncated:
            header += " ⚠ truncated"
        parts = []
        for i, s in enumerate(d.stages):
            label = f"{s.display}:{s.count}"
            if s.oldest_age:
                label += f"({s.oldest_age})"
            if s.unserviced:
                label += "⚠"
            if i == self.stage_index:
                label = f"[reverse]{label}[/reverse]"
            parts.append(label)
        flow = " → ".join(parts)
        lines = [header, flow, f"PRs open: {d.pr_count}"]
        if d.callout:
            lines.append(f"▲ {d.callout}")
        self.update("\n".join(lines))

    @property
    def selected_stage(self) -> data.FlowStage | None:
        if not self.detail or not (0 <= self.stage_index < len(self.detail.stages)):
            return None
        return self.detail.stages[self.stage_index]


class IssueList(ListView):
    """Issues in the currently selected stage of the currently selected repo."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rows: list[dict[str, Any]] = []
        self.repo: str | None = None

    def on_mount(self) -> None:
        self.border_title = "issues"

    def set_issues(self, repo: str | None, issues: list[dict[str, Any]]) -> None:
        self.repo = repo
        self.rows = issues
        self.clear()
        if not issues:
            self.append(ListItem(Label("(none)")))
            return
        for issue in issues:
            self.append(ListItem(Label(_issue_line(issue))))
        _select_first(self)

    @property
    def selected_number(self) -> int | None:
        if not self.rows or self.index is None or not (0 <= self.index < len(self.rows)):
            return None
        number = self.rows[self.index].get("number")
        return int(number) if number is not None else None


class RunsPane(ListView):
    """Local runs for the project the TUI was launched in (`agent runs`)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rows: list[runs.Run] = []

    def on_mount(self) -> None:
        self.border_title = "runs (local checkout)"

    def set_rows(self, rows: list[runs.Run]) -> None:
        self.rows = rows
        self.clear()
        if not rows:
            self.append(ListItem(Label("(no local runs)")))
            return
        for r in rows:
            self.append(ListItem(Label(f"#{r.issue}  {r.state:<8}  {r.detail}")))
        _select_first(self)

    @property
    def selected(self) -> runs.Run | None:
        if not self.rows or self.index is None or not (0 <= self.index < len(self.rows)):
            return None
        return self.rows[self.index]


class WaitingPane(Static):
    def on_mount(self) -> None:
        self.border_title = "waiting on you"
        self.update("loading…")

    def set_summary(self, w: data.WaitingOnYou) -> None:
        lines = [
            f"{w.needs_human}  needs-human",
            f"{w.open_prs}  open PRs",
            f"{w.halted_runs}  halted runs",
            f"{w.unserviced_repos}  unserviced ⚠",
        ]
        if w.unreadable_repos:
            lines.append(f"⚠ unreadable: {', '.join(w.unreadable_repos)}")
        self.update("\n".join(lines))


class CommandBar(Static):
    def on_mount(self) -> None:
        self.update(self._legend())

    @staticmethod
    def _legend() -> str:
        return "↑↓ move · tab pane · ←→ stage · d dispatch · r resume · o open web · q quit"

    def set_preview(
        self,
        *,
        issue: int | None,
        repo: str | None,
        local: bool,
        status_message: str,
    ) -> None:
        if issue is None:
            lines = ["(select an issue)"]
        else:
            dcmd = " ".join(data.dispatch_command(issue))
            rcmd = " ".join(data.resume_command(issue))
            ocmd = " ".join(data.open_web_command(repo or "?", issue))
            note = "" if local else "  (not the local checkout — dispatch/resume unavailable)"
            lines = [
                f"d dispatch → {dcmd}{note}",
                f"r resume   → {rcmd}{note}",
                f"o open web → {ocmd}",
            ]
        lines.append(self._legend())
        if status_message:
            lines.append(status_message)
        self.update("\n".join(lines))


class TuiApp(App[None]):
    """One screen for the whole pipeline (issue #232)."""

    CSS = """
    Screen {
        layout: vertical;
    }
    #body {
        layout: grid;
        grid-size: 2 2;
        grid-columns: 1fr 2fr;
        grid-rows: 2fr 1fr;
        height: 1fr;
    }
    #body.narrow {
        layout: vertical;
        height: auto;
    }
    #body.narrow > * {
        height: auto;
        max-height: 10;
    }
    ReposPane, StageRow, IssueList, WaitingPane, RunsPane {
        border: round $panel;
        height: 100%;
    }
    #flow-wrap {
        height: 100%;
    }
    StageRow {
        height: auto;
        min-height: 5;
    }
    IssueList {
        height: 1fr;
    }
    CommandBar {
        height: auto;
        dock: bottom;
        background: $panel;
        padding: 0 1;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("tab", "focus_next", "pane", show=False),
        Binding("shift+tab", "focus_previous", "pane", show=False),
        Binding("d", "dispatch", "dispatch"),
        Binding("r", "resume", "resume"),
        Binding("o", "open_web", "open web"),
    ]

    def __init__(self, project_root: Path) -> None:
        super().__init__()
        self.project_root = project_root
        self.local_slug = github.remote_slug(project_root)
        self.fleet: list[data.FleetRepo] = []
        self.local_run_rows: list[runs.Run] = []
        self.load_error: str | None = None
        self.status_message = ""

    def compose(self) -> ComposeResult:
        with Container(id="body"):
            yield ReposPane(id="repos")
            with Vertical(id="flow-wrap"):
                yield StageRow(id="stagerow")
                yield IssueList(id="issues")
            yield WaitingPane(id="waiting")
            yield RunsPane(id="runs")
        yield CommandBar(id="bar")

    def on_mount(self) -> None:
        self._apply_layout(self.size.width)
        self.run_worker(self._load, thread=True, exclusive=True)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_layout(event.size.width)

    def _apply_layout(self, width: int) -> None:
        body = self.query_one("#body", Container)
        body.set_class(width < NARROW_WIDTH, "narrow")

    # --- data loading (blocking gh/git calls run off the UI thread) -----------

    def _load(self) -> None:
        try:
            config = registry.load_registry()
        except FileNotFoundError as exc:
            self.call_from_thread(self._show_load_error, str(exc))
            return
        fleet = data.load_fleet(config)
        local_rows, _trustworthy = data.local_runs(self.project_root)
        self.call_from_thread(self._apply_load, fleet, local_rows)

    def _show_load_error(self, message: str) -> None:
        self.load_error = message
        self.query_one(WaitingPane).update(f"⚠ {message}")

    def _apply_load(self, fleet: list[data.FleetRepo], local_rows: list[runs.Run]) -> None:
        self.fleet = fleet
        self.local_run_rows = local_rows
        repos_pane = self.query_one(ReposPane)
        repos_pane.set_rows([data.repo_summary(fr) for fr in fleet])
        self.query_one(WaitingPane).set_summary(data.waiting_on_you(fleet, local_rows))
        self.query_one(RunsPane).set_rows(local_rows)
        if fleet:
            self._show_repo(0, reset_stage=True)
        self._refresh_bar()
        repos_pane.focus()

    # --- selection plumbing ----------------------------------------------------

    def _show_repo(self, index: int, *, reset_stage: bool) -> None:
        if not (0 <= index < len(self.fleet)):
            return
        fr = self.fleet[index]
        running = any(r.state == "running" for r in self.local_run_rows)
        detail = data.repo_detail(fr, is_local=fr.repo == self.local_slug, local_running=running)
        stage_row = self.query_one(StageRow)
        stage_row.set_detail(detail, reset_stage=reset_stage)
        self._refresh_issue_list()

    def _refresh_issue_list(self) -> None:
        stage_row = self.query_one(StageRow)
        issue_list = self.query_one(IssueList)
        detail = stage_row.detail
        stage = stage_row.selected_stage
        if detail is None or stage is None:
            issue_list.set_issues(None, [])
            return
        matching = [i for i in detail.issues if data.issue_stage(i) == stage.key]
        issue_list.set_issues(detail.repo, matching)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "repos":
            repos_pane = self.query_one(ReposPane)
            if repos_pane.index is not None:
                self._show_repo(repos_pane.index, reset_stage=True)
        self._refresh_bar()

    def _current_selection(self) -> tuple[int | None, str | None]:
        """(issue, repo) — from whichever pane last moved: issues or runs."""
        focused = self.focused
        if isinstance(focused, RunsPane):
            run = focused.selected
            if run is not None:
                return run.issue, self.local_slug
        issue_list = self.query_one(IssueList)
        number = issue_list.selected_number
        if number is not None:
            return number, issue_list.repo
        runs_pane = self.query_one(RunsPane)
        run = runs_pane.selected
        if run is not None:
            return run.issue, self.local_slug
        return None, None

    def _refresh_bar(self) -> None:
        issue, repo = self._current_selection()
        self.query_one(CommandBar).set_preview(
            issue=issue,
            repo=repo,
            local=repo is not None and repo == self.local_slug,
            status_message=self.status_message,
        )

    # --- StageRow's left/right posts a message we react to on the app --------

    def on_stage_row_stage_changed(self, event: StageRow.StageChanged) -> None:
        self._refresh_issue_list()
        self._refresh_bar()

    # --- actions ----------------------------------------------------------------

    def action_dispatch(self) -> None:
        self._run_action(data.dispatch_command, require_local=True)

    def action_resume(self) -> None:
        self._run_action(data.resume_command, require_local=True)

    def action_open_web(self) -> None:
        issue, repo = self._current_selection()
        if issue is None or repo is None:
            self._set_status("no issue selected")
            return
        self._exec(data.open_web_command(repo, issue))

    def _run_action(self, builder: Callable[[int], list[str]], *, require_local: bool) -> None:
        issue, repo = self._current_selection()
        if issue is None:
            self._set_status("no issue selected")
            return
        if require_local and repo != self.local_slug:
            self._set_status(f"#{issue} is in {repo} — not this checkout, cannot run here")
            return
        self._exec(builder(issue))

    def _exec(self, cmd: list[str]) -> None:
        self._set_status(f"running: {' '.join(cmd)}")
        self.run_worker(partial(self._exec_worker, cmd), thread=True)

    def _exec_worker(self, cmd: list[str]) -> None:
        # check=False: a timeout, a missing binary and a non-zero exit all come
        # back as a plain CompletedProcess (utils.run's contract) rather than
        # raising, so returncode alone tells the whole story here.
        proc = run_cmd(cmd, cwd=self.project_root, check=False, timeout=120.0)
        if proc.returncode == 0:
            self.call_from_thread(self._set_status, f"ok: {' '.join(cmd)}")
        else:
            detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
            self.call_from_thread(self._set_status, f"failed: {detail}")

    def _set_status(self, message: str) -> None:
        self.status_message = message
        self._refresh_bar()
