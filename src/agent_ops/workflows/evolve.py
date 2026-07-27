"""Evidence layer for the evolve pass (#151): gather a lane's recent run history.

Deterministic, no model in the loop — this module fetches from `gh` and local
`.agent-runs/` records and reduces them to a survey table and a baseline for
the CLI to print. The prompt, the verdict, and the PR the evolve pass writes
from this evidence are #152/#153, not here.

Two things this deliberately does not surface, verified absent rather than
merely unimplemented:
- Per-run cost: recorded nowhere — `RunResult` has no cost field.
- Claude session transcripts (`~/.claude/projects/**/*.jsonl`): do not survive
  a CI runner, so there is nothing on disk to read them from in that context.

`.agent-runs/` outcome records are pruned at `runs.ARTIFACT_TTL_S` (7 days), so
even a wide `--window-days` sees at most a week of local evidence — the rest
has already aged out by the time this reads it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_ops import stubs
from agent_ops.runs import Outcome, read_outcome
from agent_ops.status import FAILED_CONCLUSIONS
from agent_ops.utils import CommandError, run
from agent_ops.workflows.spec import ESCALATION_HEADER

# Which lane's escalations look like what. Spec is the only lane that posts
# one today (`workflows/spec.py`); a second lane adding one extends this map,
# not the code that reads it.
LANE_ESCALATION_HEADERS: dict[str, str] = {"spec": ESCALATION_HEADER}

# `gh issue list --json comments` returns full comment bodies for every issue
# it lists, so this is bounded rather than left to whatever `--limit` a caller
# passes for the (much smaller) PR/run lists.
_ISSUE_LIMIT = 100

_OUTCOME_FILE_RE = re.compile(r"^issue-(\d+)-outcome\.json$")
_ISSUE_BRANCH_RE = re.compile(r"^fix/issue-\d+$")


@dataclass(frozen=True)
class SurveyRow:
    when: datetime
    lane: str
    trigger: str
    conclusion: str
    duration: str
    url: str
    source: str  # ci | pr | escalation | local


@dataclass(frozen=True)
class Baseline:
    lane: str
    window_days: int
    runs: int
    ci_failures: int
    ci_failure_rate: float
    escalations: int
    prs_merged: int
    prs_closed: int


def _parse_dt(text: str | None) -> datetime | None:
    """An ISO8601 `gh` timestamp (`...Z`) as an aware `datetime`, or None."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _duration(start: datetime | None, end: datetime | None) -> str:
    """A short human span between two timestamps, or "-" when either is missing."""
    if start is None or end is None:
        return "-"
    seconds = int((end - start).total_seconds())
    if seconds < 0:
        return "-"
    hours, minutes = divmod(seconds // 60, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m" if minutes else "<1m"


def _pr_lane(branch: str) -> str:
    """The lane a PR's branch attributes to it — a convention, not a fact.

    Only `fix/issue-N` (the branch `implement`/`resume` create) is attributed;
    everything else is generic `pr` rather than guessed at.
    """
    return "implement" if _ISSUE_BRANCH_RE.match(branch) else "pr"


def _fetch_ci_runs(root: Path, lane: str, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """CI runs for every one of the lane's caller workflows, or `[]` when it has none.

    Filenames vary per repo, so the caller files are found by content
    (`stubs.caller_workflows`) rather than guessed as `<lane>.yml`. A lane can
    be split across more than one caller (`lane_caller_drift` handles that
    case too), so every caller is queried and merged rather than just the
    first — otherwise a repo that splits a lane loses half its CI history.

    The second element is True when any one caller's `gh run list` came back
    with exactly `limit` rows — a sign that call was truncated and the window
    it fed understates the truth.

    Fails open (drops that caller's runs) on any `gh` failure — non-zero
    exit, timeout, missing binary, or bad JSON — same as every other fetch
    here; a survey with fewer rows than the truth beats a crash.
    """
    callers = stubs.caller_workflows(root, lane)
    runs: list[dict[str, Any]] = []
    truncated = False
    for caller in callers:
        try:
            proc = run(
                [
                    "gh",
                    "run",
                    "list",
                    "--workflow",
                    caller.name,
                    "--limit",
                    str(limit),
                    "--json",
                    "databaseId,event,status,conclusion,createdAt,updatedAt,url",
                ],
                cwd=root,
                check=False,
            )
            if proc.returncode != 0:
                continue
            batch = json.loads(proc.stdout)
        except (CommandError, OSError, json.JSONDecodeError):
            continue
        runs.extend(batch)
        if len(batch) == limit:
            truncated = True
    return runs, truncated


def _fetch_prs(root: Path, limit: int) -> tuple[list[dict[str, Any]], bool]:
    """Fails open (`[]`) on any `gh` failure — see `_fetch_ci_runs`.

    The second element is True when the result came back with exactly
    `limit` rows — a sign the fetch was truncated, same as `_fetch_ci_runs`.
    """
    try:
        proc = run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "all",
                "--limit",
                str(limit),
                "--json",
                "number,headRefName,state,createdAt,closedAt,mergedAt,url",
            ],
            cwd=root,
            check=False,
        )
        if proc.returncode != 0:
            return [], False
        prs = json.loads(proc.stdout)
    except (CommandError, OSError, json.JSONDecodeError):
        return [], False
    return prs, len(prs) == limit


def _fetch_escalations(root: Path, limit: int = _ISSUE_LIMIT) -> tuple[list[dict[str, Any]], bool]:
    """Fails open (`[]`) on any `gh` failure — non-zero exit, timeout, missing binary,
    or bad JSON — same as `github.open_prs_for_issue`.

    The second element is True when the result came back with exactly
    `limit` rows — a sign the fetch was truncated, same as `_fetch_ci_runs`.
    Because `gh issue list` returns newest-*created* first, a truncated fetch
    can drop a recent escalation comment on an old issue entirely.
    """
    try:
        proc = run(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "all",
                "--limit",
                str(limit),
                "--json",
                "number,url,comments",
            ],
            cwd=root,
            check=False,
        )
        if proc.returncode != 0:
            return [], False
        issues = json.loads(proc.stdout)
    except (CommandError, OSError, json.JSONDecodeError):
        return [], False
    return issues, len(issues) == limit


def _load_local_outcomes(root: Path) -> list[Outcome]:
    """Every readable local outcome record under `.agent-runs/`, via `runs.read_outcome`."""
    runs_dir = root / ".agent-runs"
    if not runs_dir.is_dir():
        return []
    outcomes: list[Outcome] = []
    for path in sorted(runs_dir.iterdir()):
        match = _OUTCOME_FILE_RE.match(path.name)
        if match is None:
            continue
        outcome = read_outcome(root, int(match.group(1)))
        if outcome is not None:
            outcomes.append(outcome)
    return outcomes


def build_survey(
    *,
    lane: str,
    ci_runs: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    outcomes: list[Outcome],
    now: datetime,
    window_days: int,
) -> list[SurveyRow]:
    """Normalise every source into rows for `lane`, newest first, within the window.

    Each source attributes its own rows to a lane and only `lane`'s rows make
    it into the result: CI runs are already fetched for this lane's workflow;
    a PR's lane comes from its branch (`_pr_lane`); an escalation only counts
    if its header matches `LANE_ESCALATION_HEADERS[lane]`; a local outcome
    record always attributes to `implement` (the convention `_pr_lane` also
    uses — the record carries no lane of its own). A row whose timestamp is
    missing, unparseable, or older than `window_days` is dropped rather than
    raising: this is a best-effort survey over data an external tool produced,
    not something whose absence should fail the read.
    """
    cutoff = now - timedelta(days=window_days)
    rows: list[SurveyRow] = []

    for item in ci_runs:
        created = _parse_dt(item.get("createdAt"))
        if created is None or created < cutoff:
            continue
        updated = _parse_dt(item.get("updatedAt"))
        rows.append(
            SurveyRow(
                when=created,
                lane=lane,
                trigger=str(item.get("event") or "?"),
                conclusion=str(item.get("conclusion") or item.get("status") or "?"),
                duration=_duration(created, updated),
                url=str(item.get("url") or ""),
                source="ci",
            )
        )

    for pr in prs:
        row_lane = _pr_lane(str(pr.get("headRefName") or ""))
        if row_lane != lane:
            continue
        merged_at = _parse_dt(pr.get("mergedAt"))
        closed_at = _parse_dt(pr.get("closedAt"))
        when = merged_at or closed_at or _parse_dt(pr.get("createdAt"))
        if when is None or when < cutoff:
            continue
        if merged_at is not None:
            conclusion = "merged"
        elif str(pr.get("state") or "").upper() == "CLOSED":
            conclusion = "closed"
        else:
            conclusion = "open"
        rows.append(
            SurveyRow(
                when=when,
                lane=row_lane,
                trigger="pr",
                conclusion=conclusion,
                duration="-",
                url=str(pr.get("url") or ""),
                source="pr",
            )
        )

    header = LANE_ESCALATION_HEADERS.get(lane)
    if header is not None:
        for issue in issues:
            for comment in issue.get("comments") or []:
                body = (comment.get("body") or "").lstrip()
                if not body.startswith(header):
                    continue
                when = _parse_dt(comment.get("createdAt"))
                if when is None or when < cutoff:
                    continue
                rows.append(
                    SurveyRow(
                        when=when,
                        lane=lane,
                        trigger="issue",
                        conclusion="escalated",
                        duration="-",
                        url=str(issue.get("url") or ""),
                        source="escalation",
                    )
                )

    if lane == "implement":
        for outcome in outcomes:
            if outcome.finished_at is None:
                continue
            when = datetime.fromtimestamp(outcome.finished_at, tz=UTC)
            if when < cutoff:
                continue
            rows.append(
                SurveyRow(
                    when=when,
                    lane="implement",
                    trigger="local",
                    conclusion=outcome.state,
                    duration="-",
                    url=outcome.pr_url or "",
                    source="local",
                )
            )

    rows.sort(key=lambda row: row.when, reverse=True)
    return rows


def baseline(rows: list[SurveyRow], *, lane: str, window_days: int) -> Baseline:
    """Window-level metrics over an already-built survey. Independent of `build_survey`.

    An `implement` run that opens a PR produces both a `pr` row and a `local`
    row (from its `.agent-runs/issue-N-outcome.json`) for the same run; the
    `local` row is dropped from the run count when a `pr` row shares its url
    (the outcome record carries `pr_url`), so the count reflects distinct
    runs rather than distinct rows. An escalation comment annotates a run
    that already produced its own `ci` row — it carries the issue's url, not
    the run's, so it can't be deduped by url — and is excluded from the
    count outright rather than counted as a second run.
    """
    pr_urls = {r.url for r in rows if r.source == "pr" and r.url}
    counted = [
        r
        for r in rows
        if r.source != "escalation" and not (r.source == "local" and r.url in pr_urls)
    ]
    ci_rows = [r for r in rows if r.source == "ci"]
    ci_failures = sum(1 for r in ci_rows if r.conclusion in FAILED_CONCLUSIONS)
    ci_failure_rate = ci_failures / len(ci_rows) if ci_rows else 0.0
    escalations = sum(1 for r in rows if r.source == "escalation")
    prs_merged = sum(1 for r in rows if r.source == "pr" and r.conclusion == "merged")
    prs_closed = sum(1 for r in rows if r.source == "pr" and r.conclusion == "closed")
    return Baseline(
        lane=lane,
        window_days=window_days,
        runs=len(counted),
        ci_failures=ci_failures,
        ci_failure_rate=ci_failure_rate,
        escalations=escalations,
        prs_merged=prs_merged,
        prs_closed=prs_closed,
    )


def render_survey(rows: list[SurveyRow]) -> str:
    """Column-sized table: timestamp, lane, trigger, conclusion, duration, url."""
    if not rows:
        return "(no rows)"
    cells = [
        (
            row.when.strftime("%Y-%m-%d %H:%M"),
            row.lane,
            row.trigger,
            row.conclusion,
            row.duration,
            row.url,
        )
        for row in rows
    ]
    widths = [max(len(cell[i]) for cell in cells) for i in range(5)]
    return "\n".join(
        "  ".join(cell[i].ljust(widths[i]) for i in range(5)) + f"  {cell[5]}" for cell in cells
    )


def render_baseline(b: Baseline) -> str:
    return (
        f"baseline for {b.lane} over the last {b.window_days}d: {b.runs} run(s), "
        f"CI failure rate {b.ci_failure_rate:.0%} ({b.ci_failures} failed), "
        f"{b.escalations} escalation(s), PRs merged {b.prs_merged} / closed {b.prs_closed}"
    )


def gather(
    root: Path, lane: str, *, now: datetime, window_days: int, limit: int = 100
) -> tuple[list[SurveyRow], list[str]]:
    """Fetch every source and build `lane`'s survey — the only function here to hit the network.

    Only fetches a source that can actually attribute a row to `lane`:
    `_fetch_prs`/`_load_local_outcomes` only ever produce `implement` rows
    (`_pr_lane`), and `_fetch_escalations` only produces rows for lanes in
    `LANE_ESCALATION_HEADERS` — every other lane would pay for a fetch (the
    escalation one is an expensive `gh issue list --json comments` GraphQL
    N+1) whose result `build_survey` discards.

    Returns the survey rows plus any notes worth surfacing to the caller: a
    warning when a fetch was truncated by its limit and the baseline built
    from it understates the window, or — when `lane` has no caller workflow
    and no other source applies — that it cannot be surveyed at all. That
    last case must not be confused with a real source that simply saw zero
    runs: "no evidence exists" and "no evidence source exists" are different
    findings, and only the caller (`cli.evolve`) can tell them apart if
    `gather` reports them identically.
    """
    ci_runs, ci_truncated = _fetch_ci_runs(root, lane, limit)
    prs, prs_truncated = _fetch_prs(root, limit) if lane in ("implement", "pr") else ([], False)
    issues, issues_truncated = (
        _fetch_escalations(root) if lane in LANE_ESCALATION_HEADERS else ([], False)
    )
    outcomes = _load_local_outcomes(root) if lane == "implement" else []
    rows = build_survey(
        lane=lane,
        ci_runs=ci_runs,
        prs=prs,
        issues=issues,
        outcomes=outcomes,
        now=now,
        window_days=window_days,
    )
    notes = []
    if ci_truncated:
        notes.append(
            f"CI run list hit the {limit}-run fetch limit for at least one workflow — "
            f"the {window_days}d baseline may understate the true window"
        )
    if prs_truncated:
        notes.append(
            f"PR list hit the {limit}-PR fetch limit — prs_merged/prs_closed may "
            f"understate the {window_days}d baseline"
        )
    if issues_truncated:
        notes.append(
            f"issue list hit the {_ISSUE_LIMIT}-issue fetch limit — escalations may "
            f"understate the {window_days}d baseline, and because `gh issue list` "
            f"returns newest-created first, a recent escalation on an older issue "
            f"may be missing entirely"
        )
    other_source = lane in ("implement", "pr") or lane in LANE_ESCALATION_HEADERS
    if not other_source and not stubs.caller_workflows(root, lane):
        notes.append(
            f"{lane!r} has no evidence source: no CI caller workflow, PR/escalation "
            f"mapping, or local outcome convention exists for it — this is not the "
            f"same as a lane with zero recent runs"
        )
    return rows, notes
