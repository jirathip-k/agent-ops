"""The pipeline TUI (issue #232): one screen for status/runs/dispatch/resume,
with every action showing the command it is about to run.

Read-only by default; `d`/`r`/`o` are the only actions, and each shells out to
the real CLI command it displays — never a re-implementation of dispatch or
resume logic. No new state: everything comes from `agent_ops.tui.data`, which
itself is a thin wrapper over `status.py`/`runs.py`.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from textual import events, markup
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Label, ListItem, ListView, Static

from agent_ops import github, registry, runs, status
from agent_ops.config import ProjectConfig, load_project_config
from agent_ops.tui import data, sinks
from agent_ops.utils import CommandError
from agent_ops.utils import run as run_cmd

# Below this width the 2x2 grid no longer has room to stay side-by-side —
# panes stack instead, per the narrow-terminal layout in #232's design. Set
# under 80 so the ordinary 80-column terminal (#232's stated baseline) still
# gets the wide grid; something narrower (a phone over SSH) gets the stack.
NARROW_WIDTH = 76


# Labels that mark an issue as stuck on a human rather than an agent — not
# the platform's full label vocabulary (`blocked` isn't one `status.py`
# knows), just the ones worth calling out in the issue list (#248).
_ATTENTION_LABELS = ("needs-human", "blocked")


def _issue_line(issue: dict[str, Any]) -> str:
    number = issue.get("number", "?")
    age = status._age(issue["createdAt"]) if issue.get("createdAt") else "?"
    line = f"#{number}  {age}"
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    hits = [name for name in _ATTENTION_LABELS if name in labels]
    if hits:
        line += f"  [$warning]⚠ {', '.join(hits)}[/]"
    return line


def _select_first(list_view: ListView) -> None:
    """Highlight row 0 as soon as a list has real content.

    `ListView.index` otherwise stays `None` until the user presses an arrow
    key inside it (Textual's own default), which would leave the command bar
    reading "(select an issue)" for a repo with exactly one dispatchable
    issue — the opposite of "the command is visible."
    """
    list_view.index = 0


class ReposPane(ListView):
    """Fleet repo list — always-visible ⚠ marker for an unserviced stage (#229).

    Rows render incrementally (#232's post-review fix): the fleet sweep is
    concurrent now, so repos finish in whatever order `gh` answers, not
    registry order. `show_loading` lays out every row up front so a repo not
    yet fetched shows `loading…` at its fixed position; `update_row` then
    fills one row in place without touching the others or the list's
    selection — `clear()` resets `ListView.index`, which would post a spurious
    `Highlighted` and reset whatever stage the user has navigated to on the
    row they're actually looking at, every time an unrelated repo arrives.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.repo_names: list[str] = []
        self.rows: list[data.RepoSummary | None] = []
        self.display_names: dict[str, str] = {}

    def on_mount(self) -> None:
        self.border_title = "Repos"

    def show_loading(self, repos: list[str]) -> None:
        self.repo_names = list(repos)
        self.rows = [None] * len(repos)
        self.display_names = data.repo_display_names(repos)
        self.clear()
        if not repos:
            self.append(ListItem(Label("(no repos registered)")))
            return
        for repo in repos:
            self.append(ListItem(Label(f"{self.display_names[repo]}  loading…")))
        _select_first(self)

    def update_row(self, index: int, row: data.RepoSummary) -> None:
        if not (0 <= index < len(self.rows)):
            return
        self.rows[index] = row
        items = list(self.children)
        if index < len(items):
            items[index].query_one(Label).update(self._line(row))

    def _line(self, row: data.RepoSummary) -> str:
        name = self.display_names.get(row.repo, row.repo)
        if not row.readable:
            return f"{name}  ⚠ unreadable"
        count = f"≥{row.total_open}" if row.truncated else str(row.total_open)
        marker = " ⚠" if row.unserviced else ""
        return f"{name}  {count}{marker}"


class StageRow(Static):
    """The flow line — focusable; left/right pick a stage to filter issues by."""

    can_focus = True
    BINDINGS = [
        Binding("left", "prev_stage", "◀ stage", show=False),
        Binding("right", "next_stage", "▶ stage", show=False),
        # Only fires while StageRow itself is focused (#253) — mirrors how
        # left/right above only move the stage cursor while this row has
        # focus, rather than a global App binding like d/r/o/c.
        Binding("t", "trigger", "trigger", show=False),
    ]

    class StageChanged(Message):
        """Posted when left/right pick a different stage to filter issues by."""

        def __init__(self, stage_index: int) -> None:
            super().__init__()
            self.stage_index = stage_index

    class Trigger(Message):
        """Posted by `t`: run the lane servicing the currently selected stage.

        Carries nothing beyond "it happened" — the app reads the stage/repo
        to act on straight off this same `StageRow` (`selected_stage`,
        `detail.repo`), exactly as `StageChanged` above already does."""

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

    def action_trigger(self) -> None:
        self.post_message(self.Trigger())

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

    def set_awaiting(self, repo: str) -> None:
        """The selected repo's own fetch hasn't finished yet — distinct from
        no repo being selected at all."""
        self.detail = None
        self.update(f"{repo}\nloading…")

    def render_detail(self) -> None:
        d = self.detail
        if d is None:
            self.update("(no repo selected)")
            return
        if not d.readable:
            self.update(f"{d.repo}\n⚠ unreadable — could not list this repo's issues")
            return
        # The true open-issue count, not the flow stages' subtotal: `needs-human`
        # and `triage:done` issues are real open issues but aren't in FLOW_ORDER,
        # so summing `s.count` here undercounted and mislabeled the shortfall as
        # "open issue(s)" (#232's post-review fix — the #151 shape).
        total = len(d.issues)
        count = f"≥{total}" if d.truncated else str(total)
        header = f"{d.repo} — {count} open issue(s)"
        parts = []
        for i, s in enumerate(d.stages):
            label = f"{s.display}:{s.count}"
            if s.oldest_age:
                label += f"({s.oldest_age})"
            if s.unserviced:
                label += "⚠"
                label = f"[$error]{label}[/]"
            if i == self.stage_index:
                label = f"[reverse]{label}[/reverse]"
            parts.append(label)
        flow = " → ".join(parts)
        if not d.prs_readable:
            pr_line = "PRs open: ?"
        elif d.prs_truncated:
            pr_line = f"PRs open: ≥{d.pr_count}"
        else:
            pr_line = f"PRs open: {d.pr_count}"
        lines = [header, flow, pr_line]
        if d.callout:
            lines.append(f"[$warning]▲ {d.callout}[/]")
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


class IssueDetailPane(VerticalScroll):
    """Body/labels/age/PR status for the selected issue (#235) — fetched
    lazily on selection and cached by the app for the session.

    `Static(markup=False)` is the inertness guarantee this pane exists to
    provide: an issue body is untrusted content (#141/#191), and Textual
    markup can embed actions (`@click=...`); rendering it literally is what
    keeps it "render, never act" rather than merely "usually harmless".
    Wrapped in `VerticalScroll` rather than reflowing the pane to fit a long
    body — the issue is explicit that content length must not resize panes.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._body = Static(markup=False)

    def compose(self) -> ComposeResult:
        yield self._body

    def on_mount(self) -> None:
        self.border_title = "detail"
        self.show_awaiting()

    def show_awaiting(self) -> None:
        self._body.update("(select an issue)")

    def show_loading(self, number: int) -> None:
        self._body.update(f"#{number}\nloading…")

    def show_detail(self, detail: data.IssueDetail) -> None:
        if not detail.readable:
            self._body.update(f"#{detail.number}\n⚠ could not fetch this issue")
            return
        labels = ", ".join(detail.labels) if detail.labels else "(none)"
        pr = "?" if detail.has_open_pr is None else "yes" if detail.has_open_pr else "no"
        header = (
            f"#{detail.number}  {detail.title}\n{detail.age} old · labels: {labels} · open PR: {pr}"
        )
        self._body.update(f"{header}\n\n{detail.body or '(no description)'}")


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

    def show_loading(self, total: int) -> None:
        self.update(f"loading… 0/{total} repos" if total else "loading…")

    def set_summary(self, w: data.WaitingOnYou, *, loaded: int, total: int) -> None:
        open_prs = f"≥{w.open_prs}" if w.open_prs_incomplete else str(w.open_prs)
        # Colour is added alongside the text, never instead of it: the number
        # and label are what NO_COLOR/2-colour terminals fall back to.
        all_clear = w.needs_human == 0 and w.halted_runs == 0 and w.unserviced_repos == 0

        def _count_line(count: int, label: str) -> str:
            text = f"{count}  {label}"
            if count > 0:
                return f"[$warning]{text}[/]"
            if all_clear:
                return f"[$success]{text}[/]"
            return text

        lines = [
            _count_line(w.needs_human, "needs-human"),
            f"{open_prs}  open PRs",
            _count_line(w.halted_runs, "halted runs"),
            _count_line(w.unserviced_repos, "unserviced ⚠"),
        ]
        if w.unreadable_repos:
            # Repo names come from the registry, not `gh` output, but escape
            # them anyway before they reach a markup-enabled `Static.update` —
            # a `[` in a name must not be parsed as a markup tag.
            names = ", ".join(markup.escape(r) for r in w.unreadable_repos)
            lines.append(f"⚠ unreadable: {names}")
        if loaded < total:
            lines.append(f"(loading {loaded}/{total} repos…)")
        self.update("\n".join(lines))


class CommandBar(Static):
    def on_mount(self) -> None:
        self.update(self._legend())

    @staticmethod
    def _legend() -> str:
        return (
            "↑↓ move · tab pane · ←→ stage · d dispatch · r resume · o open web · c chat · "
            "t trigger · q quit"
        )

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
                f"c chat     → {data.chat_preview(issue)}",
            ]
        lines.append(self._legend())
        if status_message:
            lines.append(status_message)
        self.update("\n".join(lines))


@dataclass
class _PendingTrigger:
    """`t`'s in-progress choice (#253): which lane, then local or cloud.

    Nothing here is guessed — `phase == "lane"` means more than one deployed
    lane services the stage and a digit key must pick one; `phase ==
    "destination"` means the lane is settled and a digit key picks local vs
    cloud. `escape` cancels either phase. Digit keys are used for both (never
    `l`/`c`) because `c` already means chat on the App's own global bindings —
    reusing it here would make `_check_bindings` swallow the keystroke before
    it ever reached this state.
    """

    repo: str
    stage: data.FlowStage
    phase: str  # "lane" | "destination"
    lanes: tuple[str, ...] = ()
    lane: str = ""
    local_cmd: tuple[str, ...] = ()
    cloud_cmd: tuple[str, ...] = ()
    caller_file: str = ""


class TuiApp(App[Path | None]):
    """One screen for the whole pipeline (issue #232).

    Returns the chat handoff path from `run()` when `c` (#235) wrote one and
    exited through it; `None` for an ordinary `q` quit.
    """

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
    ReposPane, StageRow, IssueList, IssueDetailPane, WaitingPane, RunsPane {
        border: round $panel;
        height: 100%;
    }
    ReposPane:focus, StageRow:focus, IssueList:focus, IssueDetailPane:focus, RunsPane:focus {
        border: double $accent;
    }
    #flow-wrap {
        height: 100%;
    }
    StageRow {
        height: auto;
        min-height: 5;
    }
    IssueList {
        height: 2fr;
    }
    IssueDetailPane {
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
        Binding("c", "chat", "chat"),
    ]

    def __init__(
        self,
        project_root: Path,
        *,
        theme: str = "catppuccin-macchiato",
        project_config: ProjectConfig | None = None,
    ) -> None:
        super().__init__()
        self.project_root = project_root
        self._initial_theme = theme
        self.local_slug = github.remote_slug(project_root)
        self.config = registry.RegistryConfig()
        self.project_config: ProjectConfig = (
            project_config if project_config is not None else load_project_config(project_root)
        )
        self.fleet: list[data.FleetRepo | None] = []
        self.local_run_rows: list[runs.Run] = []
        self.load_error: str | None = None
        self.status_message = ""
        self.issue_detail_cache: dict[tuple[str, int], data.IssueDetail] = {}
        self._issue_detail_inflight: set[tuple[str, int]] = set()
        self._pending_trigger: _PendingTrigger | None = None

    def compose(self) -> ComposeResult:
        with Container(id="body"):
            yield ReposPane(id="repos")
            with Vertical(id="flow-wrap"):
                yield StageRow(id="stagerow")
                yield IssueList(id="issues")
                yield IssueDetailPane(id="detail")
            yield WaitingPane(id="waiting")
            yield RunsPane(id="runs")
        yield CommandBar(id="bar")

    def on_mount(self) -> None:
        self.theme = self._initial_theme
        self._apply_layout(self.size.width)
        try:
            self.config = registry.load_registry()
        except FileNotFoundError as exc:
            self._show_load_error(str(exc))
            return
        self.fleet = [None] * len(self.config.repos)
        repos_pane = self.query_one(ReposPane)
        repos_pane.show_loading(self.config.repos)
        self.query_one(WaitingPane).show_loading(len(self.config.repos))
        repos_pane.focus()
        self.run_worker(self._load, thread=True, exclusive=True)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_layout(event.size.width)

    def _apply_layout(self, width: int) -> None:
        body = self.query_one("#body", Container)
        body.set_class(width < NARROW_WIDTH, "narrow")

    # --- data loading (blocking gh/git calls run off the UI thread) -----------
    #
    # The fleet sweep is concurrent (status.fetch_repos) and paints each repo's
    # row as its own fetch completes, rather than blanking the screen until
    # every repo answers — a `agent status --pipeline` sweep measured at 47s
    # against seven repos otherwise sat on "loading…" long enough to look
    # broken (#232's post-review fix). Local runs are fetched first, before
    # the fleet, since they're a single fast local read and both the Runs pane
    # and the waiting-on-you row can use them immediately.

    def _load(self) -> None:
        local_rows, _trustworthy = data.local_runs(self.project_root)
        self.call_from_thread(self._apply_local_runs, local_rows)
        fleet = data.load_fleet(self.config, on_repo=self._on_repo_loaded)
        self.call_from_thread(self._finish_load, fleet)

    def _show_load_error(self, message: str) -> None:
        self.load_error = message
        self.query_one(WaitingPane).update(f"⚠ {message}")

    def _apply_local_runs(self, rows: list[runs.Run]) -> None:
        self.local_run_rows = rows
        self.query_one(RunsPane).set_rows(rows)
        self._refresh_waiting_pane()

    def _on_repo_loaded(self, index: int, fr: data.FleetRepo) -> None:
        """Called from the sweep's worker thread as each repo's own fetch
        finishes — `call_from_thread` hands the update to the UI thread and
        blocks until it's applied."""
        self.call_from_thread(self._apply_repo, index, fr)

    def _apply_repo(self, index: int, fr: data.FleetRepo) -> None:
        if not (0 <= index < len(self.fleet)):
            return
        self.fleet[index] = fr
        self.query_one(ReposPane).update_row(index, data.repo_summary(fr))
        self._refresh_waiting_pane()
        repos_pane = self.query_one(ReposPane)
        if repos_pane.index == index:
            self._show_repo(index, reset_stage=True)
            self._refresh_bar()

    def _finish_load(self, fleet: list[data.FleetRepo]) -> None:
        # Only relevant when `load_fleet` didn't stream results (e.g. a test
        # double that returns the whole list at once): apply whatever `_apply_repo`
        # hasn't already painted. In the real concurrent path every entry is
        # already filled by the time this runs.
        for i, fr in enumerate(fleet):
            if self.fleet[i] is None:
                self._apply_repo(i, fr)
        self._refresh_waiting_pane()
        self._refresh_bar()

    def _refresh_waiting_pane(self) -> None:
        loaded = [fr for fr in self.fleet if fr is not None]
        w = data.waiting_on_you(loaded, self.local_run_rows)
        self.query_one(WaitingPane).set_summary(w, loaded=len(loaded), total=len(self.fleet))

    # --- selection plumbing ----------------------------------------------------

    def _show_repo(self, index: int, *, reset_stage: bool) -> None:
        if not (0 <= index < len(self.fleet)):
            return
        # A pending `t` choice (#253) names a specific stage/repo — switching
        # repos out from under it would let a stray digit keypress run the
        # wrong lane against the wrong repo.
        self._pending_trigger = None
        fr = self.fleet[index]
        stage_row = self.query_one(StageRow)
        if fr is None:
            stage_row.set_awaiting(self.config.repos[index])
            self._refresh_issue_list()
            return
        running = any(r.state == "running" for r in self.local_run_rows)
        detail = data.repo_detail(fr, is_local=fr.repo == self.local_slug, local_running=running)
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
        elif event.list_view.id == "issues":
            self._show_issue_detail()
        self._refresh_bar()

    def _prs_for(self, repo: str) -> tuple[list[dict[str, Any]], bool]:
        """`repo`'s already-fetched PR listing, plus whether it is complete
        enough to trust a non-match as "no open PR" rather than "unknown"
        (#243) — a failed or `OPEN_PR_LIMIT`-truncated listing, or a fleet
        repo whose own fetch hasn't finished yet, is not complete."""
        for fr in self.fleet:
            if fr is not None and fr.repo == repo:
                return fr.data.prs, fr.data.prs_readable and not fr.data.prs_truncated
        return [], False

    def _show_issue_detail(self) -> None:
        """Fetch lazily on selection, cached per issue for the session (#235):
        a cache hit renders immediately; a miss shows "loading…" and fetches
        off the UI thread, same as every other `gh` read in this app."""
        issue_list = self.query_one(IssueList)
        detail_pane = self.query_one(IssueDetailPane)
        number = issue_list.selected_number
        repo = issue_list.repo
        if number is None or repo is None:
            detail_pane.show_awaiting()
            return
        key = (repo, number)
        cached = self.issue_detail_cache.get(key)
        if cached is not None:
            detail_pane.show_detail(cached)
            return
        detail_pane.show_loading(number)
        if key in self._issue_detail_inflight:
            return
        self._issue_detail_inflight.add(key)
        prs, prs_complete = self._prs_for(repo)
        self.run_worker(partial(self._fetch_issue_detail, key, prs, prs_complete), thread=True)

    def _fetch_issue_detail(
        self, key: tuple[str, int], prs: list[dict[str, Any]], prs_complete: bool
    ) -> None:
        repo, number = key
        detail = data.load_issue_detail(repo, number, prs, prs_complete=prs_complete)
        self.call_from_thread(self._apply_issue_detail, key, detail)

    def _apply_issue_detail(self, key: tuple[str, int], detail: data.IssueDetail) -> None:
        self._issue_detail_inflight.discard(key)
        self.issue_detail_cache[key] = detail
        issue_list = self.query_one(IssueList)
        # The selection may have moved on while the fetch was in flight —
        # cache the result regardless, but only render it if it's still what
        # the user is looking at.
        if (issue_list.repo, issue_list.selected_number) == key:
            self.query_one(IssueDetailPane).show_detail(detail)

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

    # --- StageRow's left/right/t post messages we react to on the app --------

    def on_stage_row_stage_changed(self, event: StageRow.StageChanged) -> None:
        # Same reasoning as the repo-switch guard in `_show_repo`: a pending
        # choice names a specific stage, so moving off it invalidates it.
        self._pending_trigger = None
        self._refresh_issue_list()
        self._refresh_bar()

    def on_stage_row_trigger(self, event: StageRow.Trigger) -> None:
        self._begin_trigger()

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

    def action_chat(self) -> None:
        """`c` (#235/#249): hand the selection to a chat pane through a
        multiplexer-agnostic sink (tmux/wezterm/kitty/Orca, or
        `tui.chat_sink`), same as `d`/`r`/`o`, this shells out to nothing
        itself and hands off instead. No live pane reachable → falls back to
        writing the handoff file and exiting, exactly as before this issue."""
        issue, repo = self._current_selection()
        if issue is None or repo is None:
            self._set_status("no issue selected")
            return
        self._set_status(f"chat: {data.chat_preview(issue)}")
        key = (repo, issue)
        cached = self.issue_detail_cache.get(key)
        prs, prs_complete = self._prs_for(repo)
        self.run_worker(partial(self._chat_worker, key, cached, prs, prs_complete), thread=True)

    def _chat_worker(
        self,
        key: tuple[str, int],
        cached: data.IssueDetail | None,
        prs: list[dict[str, Any]],
        prs_complete: bool,
    ) -> None:
        repo, issue = key
        detail = (
            cached
            if cached is not None
            else data.load_issue_detail(repo, issue, prs, prs_complete=prs_complete)
        )
        text = data.render_chat_handoff(repo, detail)
        sink = sinks.deliver(
            text, self.project_config.tui.chat_sink, dict(os.environ), self.project_root
        )
        if isinstance(sink, sinks.FileSink):
            # The multiplexer already splits (#249 supersedes #240's
            # suspend-and-host-a-chat proposal) — only the file sink has
            # nowhere else to show its result, so only it ends the session.
            self.call_from_thread(self.exit, data.chat_handoff_path(self.project_root))
            return
        self.call_from_thread(self._set_status, f"chat: sent #{issue} to a {sink.name} pane")

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

    # --- `t`: trigger the lane servicing the focused stage (#253) ------------
    #
    # Local and cloud are both real entry points: local shells out to the
    # exact same `agent <lane>` CLI `d`/`r` already use (or spawns it the same
    # way `workflows/triage.py`'s own dispatch step does, for the lane-wide
    # lanes that run start-to-finish rather than taking `--surface`), and
    # cloud runs `gh workflow run` against the repo's own deployed caller —
    # never a new dispatch mechanism, never a write under `.github/workflows/`.

    # (stage.key, lane) pairs whose local command runs synchronously
    # start-to-finish inside `agent <lane>` itself (no `--surface`) rather
    # than spawning a surface and returning immediately the way `agent
    # dispatch --surface orca` / `agent plan --surface orca` do — these must
    # go through `surfaces.pick(...).spawn(...)` directly instead of
    # `_exec`'s blocking `run_cmd`, or a multi-minute triage/groom/spec sweep
    # would tie up a worker thread with no live output until it finished.
    _STREAMED_LOCAL_LANES = frozenset(
        {("untriaged", "triage"), ("backlog", "groom"), ("spec-requested", "spec")}
    )

    # Bound on the `gh run list` follow-up that tries to surface the queued
    # run's URL. `gh workflow run` itself never returns a run id (a known `gh`
    # CLI limitation) — this polls for the most recent run of that workflow
    # file a few times, and gives up with a "check `gh run list`" pointer
    # rather than blocking indefinitely for a run GitHub hasn't indexed yet.
    _CLOUD_FOLLOWUP_ATTEMPTS = 3
    _CLOUD_FOLLOWUP_DELAY_S = 2.0

    def _fleet_repo(self, repo: str) -> data.FleetRepo | None:
        return next((fr for fr in self.fleet if fr is not None and fr.repo == repo), None)

    def _begin_trigger(self) -> None:
        stage_row = self.query_one(StageRow)
        stage = stage_row.selected_stage
        detail = stage_row.detail
        if stage is None or detail is None:
            self._set_status("no stage selected")
            return
        if not stage.available_lanes:
            self._set_status(
                f"{stage.display}: nothing to trigger — {self._no_trigger_reason(stage)}"
            )
            return
        lanes = list(stage.available_lanes)
        if len(lanes) > 1:
            self._prompt_lane_choice(detail.repo, stage, lanes)
        else:
            self._resolve_destination(detail.repo, stage, lanes[0])

    @staticmethod
    def _no_trigger_reason(stage: data.FlowStage) -> str:
        if stage.count == 0:
            return "empty"
        if stage.key not in status.STAGE_CONSUMERS:
            return "no CI lane ever consumes this stage"
        return "unserviced — no deployed lane consumes this stage"

    def _prompt_lane_choice(self, repo: str, stage: data.FlowStage, lanes: list[str]) -> None:
        # Never `next(iter(...))` — a stage with more than one consumer lane
        # (STAGE_CONSUMERS values are frozensets) must ask, not guess (#253).
        self._pending_trigger = _PendingTrigger(
            repo=repo, stage=stage, phase="lane", lanes=tuple(lanes)
        )
        options = "  ".join(f"[{i + 1}] {lane}" for i, lane in enumerate(lanes))
        self._set_status(
            f"trigger {stage.display}: more than one lane services this — which? "
            f"{options}  (esc cancels)"
        )

    def _resolve_destination(self, repo: str, stage: data.FlowStage, lane: str) -> None:
        fr = self._fleet_repo(repo)
        if fr is None or fr.lanes is None or lane not in fr.lanes:
            self._pending_trigger = None
            self._set_status(f"{repo}: {lane} is no longer wired up — nothing to trigger")
            return
        caller_file = fr.lanes[lane].caller_file
        desc = data.trigger_description(stage, lane)
        cloud_cmd = data.cloud_trigger_command(repo, caller_file)
        local_cmd = (
            data.local_trigger_command(stage, lane, stage.oldest_number)
            if repo == self.local_slug
            else None
        )
        if local_cmd is not None:
            # Both are viable — ask which rather than guessing (#253).
            self._pending_trigger = _PendingTrigger(
                repo=repo,
                stage=stage,
                phase="destination",
                lane=lane,
                local_cmd=tuple(local_cmd),
                cloud_cmd=tuple(cloud_cmd),
                caller_file=caller_file,
            )
            self._set_status(
                f"trigger: {desc} — [1] local: {' '.join(local_cmd)}  "
                f"[2] cloud: {' '.join(cloud_cmd)}  (esc cancels)"
            )
            return
        # Only cloud is viable — not the local checkout, or this pairing has
        # no local command mapping — so there is nothing to ask; run it.
        self._pending_trigger = None
        self._run_cloud_trigger(desc, repo, cloud_cmd, caller_file)

    def _handle_trigger_key(self, key: str) -> None:
        """Fallback key handler for the digits/escape `t`'s prompts use.

        Textual only calls a `key_<name>` method like this once the normal
        binding chain (the focused widget's own BINDINGS, then the App's)
        has already passed on the key — digits and escape are bound nowhere
        else in this app, so this only ever fires for `t`'s own prompts, and
        is a complete no-op whenever none is open.
        """
        pending = self._pending_trigger
        if pending is None:
            return
        if key == "escape":
            self._pending_trigger = None
            self._set_status("trigger cancelled")
            return
        if not key.isdigit():
            return
        index = int(key) - 1
        if pending.phase == "lane":
            if not (0 <= index < len(pending.lanes)):
                return
            lane = pending.lanes[index]
            self._pending_trigger = None
            self._resolve_destination(pending.repo, pending.stage, lane)
            return
        # phase == "destination": 1 = local, 2 = cloud; anything else no-ops.
        if index == 0:
            self._pending_trigger = None
            self._run_local_trigger(pending.stage, pending.lane, list(pending.local_cmd))
        elif index == 1:
            desc = data.trigger_description(pending.stage, pending.lane)
            self._pending_trigger = None
            self._run_cloud_trigger(
                desc, pending.repo, list(pending.cloud_cmd), pending.caller_file
            )

    def key_escape(self) -> None:
        self._handle_trigger_key("escape")

    def key_1(self) -> None:
        self._handle_trigger_key("1")

    def key_2(self) -> None:
        self._handle_trigger_key("2")

    def key_3(self) -> None:
        self._handle_trigger_key("3")

    def key_4(self) -> None:
        self._handle_trigger_key("4")

    def key_5(self) -> None:
        self._handle_trigger_key("5")

    def key_6(self) -> None:
        self._handle_trigger_key("6")

    def key_7(self) -> None:
        self._handle_trigger_key("7")

    def key_8(self) -> None:
        self._handle_trigger_key("8")

    def _run_local_trigger(self, stage: data.FlowStage, lane: str, cmd: list[str]) -> None:
        if (stage.key, lane) in self._STREAMED_LOCAL_LANES:
            self._set_status(f"running: {' '.join(cmd)}")
            self.run_worker(partial(self._spawn_worker, f"agent-{lane}", cmd), thread=True)
            return
        self._exec(cmd)

    def _spawn_worker(self, label: str, cmd: list[str]) -> None:
        from agent_ops import surfaces  # local import: pulls in subprocess spawning

        try:
            spawned = surfaces.pick("auto").spawn(label, cmd, self.project_root)
        except CommandError as exc:
            self.call_from_thread(self._set_status, f"failed to start: {exc}")
            return
        self.call_from_thread(self._set_status, f"running: {spawned.where}")

    def _run_cloud_trigger(self, desc: str, repo: str, cmd: list[str], caller_file: str) -> None:
        self._set_status(f"queuing: {desc} → {' '.join(cmd)}")
        self.run_worker(partial(self._cloud_worker, repo, cmd, caller_file), thread=True)

    def _cloud_worker(self, repo: str, cmd: list[str], caller_file: str) -> None:
        proc = run_cmd(cmd, cwd=self.project_root, check=False, timeout=60.0)
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip() or f"exit {proc.returncode}"
            self.call_from_thread(self._set_status, f"failed to queue: {detail}")
            return
        url = self._await_run_url(repo, caller_file)
        if url:
            self.call_from_thread(self._set_status, f"queued: {' '.join(cmd)} → {url}")
        else:
            self.call_from_thread(
                self._set_status,
                f"queued: {' '.join(cmd)} — run not visible yet, check "
                f"`gh run list --repo {repo} --workflow {caller_file}`",
            )

    def _await_run_url(self, repo: str, caller_file: str) -> str | None:
        for attempt in range(self._CLOUD_FOLLOWUP_ATTEMPTS):
            proc = run_cmd(
                [
                    "gh",
                    "run",
                    "list",
                    "--repo",
                    repo,
                    "--workflow",
                    caller_file,
                    "--limit",
                    "1",
                    "--json",
                    "url,createdAt,event",
                ],
                cwd=self.project_root,
                check=False,
                timeout=30.0,
            )
            if proc.returncode == 0:
                try:
                    rows = json.loads(proc.stdout)
                except json.JSONDecodeError:
                    rows = []
                if rows and rows[0].get("url"):
                    return str(rows[0]["url"])
            if attempt < self._CLOUD_FOLLOWUP_ATTEMPTS - 1:
                time.sleep(self._CLOUD_FOLLOWUP_DELAY_S)
        return None
