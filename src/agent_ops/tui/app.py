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
from textual.containers import Container, Vertical, VerticalScroll
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
        pr = "yes" if detail.has_open_pr else "no"
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
        lines = [
            f"{w.needs_human}  needs-human",
            f"{w.open_prs}  open PRs",
            f"{w.halted_runs}  halted runs",
            f"{w.unserviced_repos}  unserviced ⚠",
        ]
        if w.unreadable_repos:
            lines.append(f"⚠ unreadable: {', '.join(w.unreadable_repos)}")
        if loaded < total:
            lines.append(f"(loading {loaded}/{total} repos…)")
        self.update("\n".join(lines))


class CommandBar(Static):
    def on_mount(self) -> None:
        self.update(self._legend())

    @staticmethod
    def _legend() -> str:
        return (
            "↑↓ move · tab pane · ←→ stage · d dispatch · r resume · o open web · c chat · q quit"
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

    def __init__(self, project_root: Path) -> None:
        super().__init__()
        self.project_root = project_root
        self.local_slug = github.remote_slug(project_root)
        self.config = registry.RegistryConfig()
        self.fleet: list[data.FleetRepo | None] = []
        self.local_run_rows: list[runs.Run] = []
        self.load_error: str | None = None
        self.status_message = ""
        self.issue_detail_cache: dict[tuple[str, int], data.IssueDetail] = {}
        self._issue_detail_inflight: set[tuple[str, int]] = set()

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

    def _prs_for(self, repo: str) -> list[dict[str, Any]]:
        for fr in self.fleet:
            if fr is not None and fr.repo == repo:
                return fr.data.prs
        return []

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
        prs = self._prs_for(repo)
        self.run_worker(partial(self._fetch_issue_detail, key, prs), thread=True)

    def _fetch_issue_detail(self, key: tuple[str, int], prs: list[dict[str, Any]]) -> None:
        repo, number = key
        detail = data.load_issue_detail(repo, number, prs)
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

    def action_chat(self) -> None:
        """`c` (#235): write the handoff file and exit through it — no
        worktree, no hosted chat, same as `d`/`r`/`o`, this shells out to
        nothing and hands off instead."""
        issue, repo = self._current_selection()
        if issue is None or repo is None:
            self._set_status("no issue selected")
            return
        self._set_status(f"chat: {data.chat_preview(issue)}")
        key = (repo, issue)
        cached = self.issue_detail_cache.get(key)
        prs = self._prs_for(repo)
        self.run_worker(partial(self._chat_worker, key, cached, prs), thread=True)

    def _chat_worker(
        self,
        key: tuple[str, int],
        cached: data.IssueDetail | None,
        prs: list[dict[str, Any]],
    ) -> None:
        repo, issue = key
        detail = cached if cached is not None else data.load_issue_detail(repo, issue, prs)
        path = data.chat_handoff_path(self.project_root)
        data.write_chat_handoff(path, repo, detail)
        self.call_from_thread(self.exit, path)

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
