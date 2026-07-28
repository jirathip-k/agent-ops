"""Read-only data gathering for the pipeline TUI (issue #232).

No new state and no new fetching path: every function here is a thin wrapper
around calls `status.py`/`runs.py`/`github.py` already make for `agent
status`/`agent runs` — the TUI reads what those commands read, structured for
a screen instead of formatted for a log line. If a number the TUI needs isn't
derivable from an existing call, that is a gap in `status`/`runs` to close
there first, not a second way to compute it here (the #150 shape).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_ops import runs, status
from agent_ops.claims import CLAIM_LABEL
from agent_ops.registry import RegistryConfig
from agent_ops.utils import CommandError

# Narrative flow order shown in the detail pane: a curated ordering of
# `status.STAGE_PRECEDENCE` for "what stage is this issue at", ending with the
# claimed ("run") stage. `needs-human` and `triage:done` are deliberately
# excluded — `needs-human` surfaces in the waiting-on-you row instead, and
# `triage:done` (classified but left bucketless) does not read as a step an
# issue moves through on its way to a PR.
FLOW_ORDER: tuple[tuple[str, str], ...] = (
    ("untriaged", "untriaged"),
    ("backlog", "backlog"),
    ("spec-requested", "spec"),
    ("plan-requested", "plan"),
    ("agent-ready", "ready"),
    (CLAIM_LABEL, "run"),
)


@dataclass(frozen=True)
class FlowStage:
    """One column of the flow row."""

    key: str
    display: str
    count: int
    oldest_age: str | None
    oldest_number: int | None
    unserviced: bool


@dataclass(frozen=True)
class RepoData:
    """One repo's raw fetch — open issues and PRs — or unreadable.

    `readable=False` is distinct from an empty fleet repo: the former means
    `gh` could not list the repo at all (404, no access, wedged network), the
    latter means it answered and there is simply nothing open.
    """

    repo: str
    readable: bool
    truncated: bool = False
    issues: list[dict[str, Any]] = field(default_factory=list)
    prs: list[dict[str, Any]] = field(default_factory=list)


def load_repo(repo: str) -> RepoData:
    try:
        issues = status._pipeline_issues(repo)
    except CommandError:
        return RepoData(repo, readable=False)
    truncated = len(issues) == status.PIPELINE_ISSUE_LIMIT
    try:
        prs = status._open_prs(repo)
    except CommandError:
        prs = []
    return RepoData(repo, readable=True, truncated=truncated, issues=issues, prs=prs)


def lanes_for(repo: str) -> dict[str, status.LaneInfo] | None:
    """Every lane `repo` has a CI caller wired up for, with runner/cron — or
    None if the workflow listing itself couldn't be read.

    Same two calls `status._deployed_lanes`/`pipeline_status` already make
    (`_repo_workflows` + `detect_lanes`), just kept as the full `LaneInfo` dict
    instead of collapsed to a bare set of names, so the cron field survives
    for the "a scheduled lane will pick this up" callout.
    """
    try:
        workflows = status._repo_workflows(repo)
    except CommandError:
        return None
    return status.detect_lanes(workflows)


@dataclass(frozen=True)
class FleetRepo:
    """One registered repo's combined fetch: issues/PRs plus lane wiring.

    Lane wiring costs an extra API call per repo, so it is only fetched when
    some stage could even be flagged unserviced — the same optimisation
    `pipeline_status` already applies.
    """

    repo: str
    data: RepoData
    lanes: dict[str, status.LaneInfo] | None


def _needs_lane_check(issues: list[dict[str, Any]]) -> bool:
    stage_entries = status.stage_counts(issues)
    return any(
        stage in status.STAGE_CONSUMERS and entry.count for stage, entry in stage_entries.items()
    )


def _load_one(repo: str) -> FleetRepo:
    repo_data = load_repo(repo)
    lanes = lanes_for(repo) if repo_data.readable and _needs_lane_check(repo_data.issues) else None
    return FleetRepo(repo, repo_data, lanes)


def load_fleet(
    config: RegistryConfig,
    *,
    on_repo: Callable[[int, FleetRepo], None] | None = None,
) -> list[FleetRepo]:
    """Every registered repo's data, fetched concurrently — the fleet-wide
    read the Repos pane, the flow detail pane and the waiting-on-you row all
    share.

    Concurrency lives in `status.fetch_repos`, not here — a serial sweep
    across seven repos measured at 47s (#232's post-review fix); a second
    concurrent-fetch path in the TUI would be the #150 shape this module's own
    docstring already rules out. `on_repo`, when given, fires as each repo's
    own fetch finishes — in completion order, paired with that repo's fixed
    index in `config.repos` — so the TUI can paint a row the moment it is
    ready without waiting for the slowest repo in the fleet, while the row's
    on-screen position stays exactly where the repo list says it belongs.
    """
    index_by_repo = {repo: i for i, repo in enumerate(config.repos)}

    def _on_result(repo: str, result: FleetRepo | CommandError) -> None:
        if on_repo is not None and not isinstance(result, CommandError):
            on_repo(index_by_repo[repo], result)

    results = status.fetch_repos(config.repos, _load_one, on_result=_on_result)
    fleet: list[FleetRepo] = []
    for _repo, result in results:
        # `_load_one` never raises — `load_repo`/`lanes_for` already catch
        # CommandError and degrade to `readable=False`/`lanes=None` — so this
        # is a type narrowing, not a real failure path.
        assert isinstance(result, FleetRepo)
        fleet.append(result)
    return fleet


def _unserviced_stages(
    stage_entries: dict[str, status.StageEntry], lanes: set[str] | None
) -> set[str]:
    if lanes is None:
        return set()
    return {
        stage
        for stage, entry in stage_entries.items()
        if entry.count
        and stage in status.STAGE_CONSUMERS
        and not status.STAGE_CONSUMERS[stage] & lanes
    }


@dataclass(frozen=True)
class RepoSummary:
    """One row of the fleet Repos pane."""

    repo: str
    readable: bool
    total_open: int | None
    truncated: bool
    unserviced: bool


def repo_display_names(repos: list[str]) -> dict[str, str]:
    """The name each repo should render as in the fleet list.

    `owner/repo` in full is the identifiable form, but the owner is usually
    the same across a fleet and the repo pane is narrow — left as full
    strings, the shared owner eats the width and the pane truncates exactly
    the part that distinguishes one row from the next (#232's post-review
    fix). Bare repo name is used wherever it alone is unique across the
    registered fleet; a name shared by two different owners keeps its
    `owner/repo` form since that's the only thing that still tells them
    apart.
    """
    bare = [repo.rsplit("/", 1)[-1] for repo in repos]
    counts: dict[str, int] = {}
    for name in bare:
        counts[name] = counts.get(name, 0) + 1
    return {
        repo: name if counts[name] == 1 else repo for repo, name in zip(repos, bare, strict=True)
    }


def repo_summary(fr: FleetRepo) -> RepoSummary:
    if not fr.data.readable:
        return RepoSummary(
            fr.repo, readable=False, total_open=None, truncated=False, unserviced=False
        )
    stage_entries = status.stage_counts(fr.data.issues)
    lane_set = set(fr.lanes) if fr.lanes is not None else None
    unserviced = bool(_unserviced_stages(stage_entries, lane_set))
    return RepoSummary(
        fr.repo,
        readable=True,
        total_open=len(fr.data.issues),
        truncated=fr.data.truncated,
        unserviced=unserviced,
    )


def _callout(
    stages: list[FlowStage],
    *,
    lanes: dict[str, status.LaneInfo] | None,
    is_local: bool,
    local_running: bool,
) -> str | None:
    """The "what to do" line — must say nothing when it cannot derive something
    true (see #232's design comment). Every string here is traceable to state
    just read above, never to an assumption about what a lane will do next.
    """
    by_key = {s.key: s for s in stages}

    # Only the current local checkout has a liveness signal (`runs.discover_runs`
    # needs a worktree on disk) — for every other fleet repo, "nothing running"
    # is not knowable, so this case is skipped rather than guessed.
    if is_local:
        ready = by_key.get("agent-ready")
        if ready is not None and ready.count > 0 and not local_running:
            return f"{ready.count} ready, nothing running"

    untriaged = by_key.get("untriaged")
    if untriaged is not None and untriaged.count > 0 and untriaged.unserviced:
        return f"{untriaged.count} untriaged, no lane deployed"

    if lanes:
        for key, lane_name in (
            ("backlog", "groom"),
            ("spec-requested", "spec"),
            ("plan-requested", "plan"),
        ):
            stage = by_key.get(key)
            info = lanes.get(lane_name)
            if stage is not None and stage.count > 0 and info is not None and info.cron is not None:
                return (
                    f"{stage.count} {stage.display}, {lane_name} is cron-scheduled — nothing to do"
                )

    return None


@dataclass(frozen=True)
class RepoDetail:
    """The flow pane's view of one repo."""

    repo: str
    readable: bool
    truncated: bool
    stages: list[FlowStage]
    pr_count: int
    issues: list[dict[str, Any]]
    prs: list[dict[str, Any]]
    callout: str | None


def repo_detail(fr: FleetRepo, *, is_local: bool, local_running: bool) -> RepoDetail:
    if not fr.data.readable:
        return RepoDetail(
            fr.repo,
            readable=False,
            truncated=False,
            stages=[],
            pr_count=0,
            issues=[],
            prs=[],
            callout=None,
        )
    stage_entries = status.stage_counts(fr.data.issues)
    lane_set = set(fr.lanes) if fr.lanes is not None else None
    stages = []
    for key, display in FLOW_ORDER:
        entry = stage_entries.get(key, status.StageEntry(0, None, None))
        age = status._age(entry.oldest_created_at) if entry.oldest_created_at else None
        unserviced = (
            lane_set is not None
            and key in status.STAGE_CONSUMERS
            and entry.count > 0
            and not status.STAGE_CONSUMERS[key] & lane_set
        )
        stages.append(FlowStage(key, display, entry.count, age, entry.oldest_number, unserviced))
    callout = _callout(stages, lanes=fr.lanes, is_local=is_local, local_running=local_running)
    return RepoDetail(
        fr.repo,
        readable=True,
        truncated=fr.data.truncated,
        stages=stages,
        pr_count=len(fr.data.prs),
        issues=fr.data.issues,
        prs=fr.data.prs,
        callout=callout,
    )


@dataclass(frozen=True)
class WaitingOnYou:
    """The row that answers "what do I do now" — first on screen by design."""

    needs_human: int
    open_prs: int
    halted_runs: int
    unserviced_repos: int
    unreadable_repos: tuple[str, ...]


def waiting_on_you(fleet: list[FleetRepo], local: list[runs.Run]) -> WaitingOnYou:
    needs_human = 0
    open_prs = 0
    unserviced_repos = 0
    unreadable: list[str] = []
    for fr in fleet:
        if not fr.data.readable:
            unreadable.append(fr.repo)
            continue
        buckets = status.bucket_counts(fr.data.issues)
        needs_human += buckets.get("needs-human", 0)
        open_prs += len(fr.data.prs)
        stage_entries = status.stage_counts(fr.data.issues)
        lane_set = set(fr.lanes) if fr.lanes is not None else None
        if _unserviced_stages(stage_entries, lane_set):
            unserviced_repos += 1
    halted_runs = sum(1 for r in local if r.state == "halted")
    return WaitingOnYou(needs_human, open_prs, halted_runs, unserviced_repos, tuple(unreadable))


# --- Actions: every binding builds the exact argv it runs, shown before it runs -----


def dispatch_command(issue: int) -> list[str]:
    return ["agent", "dispatch", str(issue), "--surface", "orca"]


def resume_command(issue: int) -> list[str]:
    return ["agent", "resume", str(issue), "--surface", "orca"]


def open_web_command(repo: str, issue: int) -> list[str]:
    # `--repo` is spelled out explicitly rather than relying on cwd: the
    # highlighted issue may belong to a fleet repo other than the one the TUI
    # was launched in, and a command preview that silently opened the wrong
    # repo's issue would be exactly the "confident wrong prompt" #232 rules out.
    return ["gh", "issue", "view", str(issue), "--repo", repo, "--web"]


def issue_stage(issue: dict[str, Any]) -> str:
    """The one stage `stage_counts` would assign this issue to — same
    precedence walk, applied to a single already-fetched issue rather than a
    whole listing, so the issue list can filter by the stage a repo's flow
    row is already showing without a second `gh` call."""
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    for stage in status.STAGE_PRECEDENCE:
        if stage in labels:
            return stage
    return "untriaged"


def local_runs(project_root: Path) -> tuple[list[runs.Run], bool]:
    return runs.discover_runs(project_root)
