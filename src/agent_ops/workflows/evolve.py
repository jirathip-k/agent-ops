"""Evidence layer (#151) and the diagnose/no-op-or-draft-PR pass (#152) for evolve.

`gather`/`baseline`/`render_survey`/`render_baseline` are deterministic, no
model in the loop — they fetch from `gh` and local `.agent-runs/` records and
reduce them to a survey table and a baseline. `run_evolve` feeds that
evidence to a planner agent, which diagnoses it against four named failure
modes and either states a reasoned no-op or proposes a change to exactly one
lane prompt (`prompts/tasks/<lane>.md`), landed as a draft, human-merge-only
PR. The scheduled cadence that invokes this on its own is #153, not here.

Two things the evidence layer deliberately does not surface, verified absent
rather than merely unimplemented:
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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_ops import github, stubs, worktree
from agent_ops.config import load_project_config
from agent_ops.fallback import run_with_fallback
from agent_ops.prompts import render_task
from agent_ops.runs import Outcome, read_outcome
from agent_ops.status import FAILED_CONCLUSIONS
from agent_ops.utils import SLOW_GIT_TIMEOUT_S, CommandError, run
from agent_ops.workflows.implement import role_request
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
        try:
            batch = json.loads(proc.stdout)
        except json.JSONDecodeError:
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
    try:
        prs = json.loads(proc.stdout)
    except json.JSONDecodeError:
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
    try:
        issues = json.loads(proc.stdout)
    except json.JSONDecodeError:
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


# --- the pass itself (#152): diagnose, no-op, or draft PR --------------------

_FAILURE_MODES = {"drift", "vagueness", "wrong focus", "fuzzy gate"}
_CITATION_RE = re.compile(r"(https?://\S+|#\d+)")
_LINE_SEP = re.compile(r"\s+[—-]+\s+")
_NOOP_RE = re.compile(r"^none\s*[—-]+\s*(.+)$", re.IGNORECASE)

# `gh label create --force` before use — same reasoning as scout's
# backlog/proposed-by-agent labels: the agent's own PR carries this label, so
# it must exist before the run that creates the PR.
_HUMAN_MERGE_ONLY_LABEL = github.Label(
    "b60205", "Opened as a draft by an automated pass — a human must review and merge"
)


@dataclass(frozen=True)
class EvolveChange:
    mode: str
    summary: str
    citations: str


@dataclass(frozen=True)
class NoopVerdict:
    reason: str


def _split_verdict_line(line: str) -> tuple[str, str, str] | None:
    """Split a `mode — summary — citations` line, tolerating a dash inside `summary`.

    A plain `split(maxsplit=2)` truncates the moment `summary` itself contains
    an internal spaced dash (e.g. "retries were added - even though the prompt
    didn't ask - see #123"): it stops after the first two separators and
    spills the rest of the summary into `citations`. Splitting mode off the
    *first* separator and citations off the *last* one instead lets `summary`
    contain as many internal dashes as it likes.
    """
    parts = _LINE_SEP.split(line, maxsplit=1)
    if len(parts) != 2:
        return None
    mode, rest = parts
    matches = list(_LINE_SEP.finditer(rest))
    if not matches:
        return None
    last = matches[-1]
    return mode, rest[: last.start()], rest[last.end() :]


def parse_evolve(text: str) -> list[EvolveChange] | NoopVerdict | None:
    """Parse the EVOLVE VERDICT block.

    `None` means no block at all (or one with nothing after the marker) —
    the caller raises on this, same as a malformed block: neither can be
    told apart from "the agent tried and produced garbage", and both must
    stay clearly separate from an explicit no-op. Only `none — <reason>`
    returns a `NoopVerdict`; everything else is one `EvolveChange` per line,
    or a `ValueError` the moment a line doesn't fit — an unknown failure
    mode, a change citing no run URL or issue/PR number, or a line that
    doesn't split into mode/summary/citations at all.
    """
    _, marker, tail = text.rpartition("EVOLVE VERDICT:")
    if not marker:
        return None
    lines = [line.strip() for line in tail.strip().splitlines() if line.strip()]
    if not lines:
        return None

    first = lines[0]
    if first.lower() == "none":
        raise ValueError("EVOLVE VERDICT: none with no reason cited")
    noop = _NOOP_RE.match(first)
    if noop:
        return NoopVerdict(noop.group(1).strip())

    changes = []
    for line in lines:
        split = _split_verdict_line(line)
        if split is None:
            raise ValueError(f"unparseable EVOLVE VERDICT line: {line!r}")
        mode, summary, citations = (p.strip() for p in split)
        if mode.lower() not in _FAILURE_MODES:
            raise ValueError(
                f"{mode!r} is not a named failure mode "
                f"(expected one of {sorted(_FAILURE_MODES)}): {line!r}"
            )
        if not _CITATION_RE.search(citations):
            raise ValueError(f"change cites no run URL or #N: {line!r}")
        changes.append(EvolveChange(mode.lower(), summary, citations))
    return changes


def _pr_body(changes: list[EvolveChange], *, baseline_block: str, notes: list[str]) -> str:
    lines = [f"- **{c.mode}** — {c.summary} ({c.citations})" for c in changes]
    parts = ["### Changes", "", *lines, "", "### Evidence baseline", "", baseline_block]
    if notes:
        parts += ["", *(f"- note: {n}" for n in notes)]
    return "\n".join(parts)


def run_evolve(
    project_root: Path,
    lane: str,
    *,
    window_days: int = 30,
    min_runs: int = 5,
    log: Callable[[str], None] = print,
) -> list[EvolveChange]:
    """Diagnose `lane`'s evidence and either open a draft PR or state a no-op.

    A no-op is the common outcome and the cheap path: below `min_runs`, or
    with an evolve PR for `lane` already open, this returns `[]` before
    spawning an agent at all — the latter also stops a second evolve PR
    stacking on an unreviewed first. The `human-merge-only` label must sync
    before anything else runs: it is the one thing standing between a draft
    PR and a mergeable one, so a failure to create or update it aborts before
    the worktree even exists, rather than after a push has already happened.

    Otherwise a planner agent reads the survey and either states `none` or
    proposes a change, and three guards stand between a change verdict and a
    push: `worktree.changed_paths` (staged, unstaged, AND untracked — not
    `git diff`, which is blind to a staged edit) must show nothing but
    `prompts/tasks/<lane>.md` — `prompts/orchestrator.md` and
    `prompts/agents/**` are outside both this allowlist and the agent's write
    grant; a change verdict paired with no changed paths is rejected as a
    contradiction, and so is a no-op verdict paired with a stray edit; and an
    unparseable verdict raises rather than being read as a no-op. The commit
    itself is then made with an explicit pathspec, so even a wrong allowlist
    check could not carry a second file into it. The PR lands as a draft
    (which `gh pr merge` refuses outright) carrying `human-merge-only`, which
    `workflows.merge.evaluate_merge` treats as a blocking violation
    independent of draft state — so marking the PR ready for review does not
    on its own make it agent-mergeable. One PR per lane per invocation.
    """
    allowed = f"prompts/tasks/{lane}.md"
    if not (project_root / allowed).is_file():
        raise RuntimeError(f"no {allowed} — {lane!r} cannot be evolved")

    config = load_project_config(project_root)
    now = datetime.now(UTC)
    rows, notes = gather(project_root, lane, now=now, window_days=window_days)
    b = baseline(rows, lane=lane, window_days=window_days)
    if b.runs < min_runs:
        log(
            f"only {b.runs} run(s) for {lane!r} in the last {window_days}d "
            f"(need {min_runs}) — no-op"
        )
        return []

    branch_prefix = f"evolve/{lane}-"
    try:
        stale_prs = [
            pr
            for pr in github.open_prs(project_root)
            if pr["headRefName"].startswith(branch_prefix)
        ]
    except CommandError as exc:
        # Fails closed, not open: swallowing this and proceeding would still
        # spend a worktree, a full planner agent run, a commit, and a push
        # before dying at `create_pr` on the same underlying `gh` failure —
        # same reasoning distill.run_distill applies to its own stale-PR
        # check (agent-ops#175). A missing/non-executable `gh` binary raises
        # this same `CommandError` (utils.run converts it under `check=True`,
        # agent-ops#154).
        raise RuntimeError(f"could not check for a stale evolve PR via `gh`: {exc}") from exc
    if stale_prs:
        pr = stale_prs[0]
        log(
            f"an open evolve PR already exists for {lane!r}: #{pr['number']} ({pr['url']}) — "
            "review or merge it before running evolve again"
        )
        return []

    try:
        sync = github.sync_labels(
            project_root,
            {"human-merge-only": _HUMAN_MERGE_ONLY_LABEL},
            repo=github.remote_slug(project_root),
        )
    except CommandError as exc:
        raise RuntimeError(f"could not sync the human-merge-only label: {exc}") from exc
    if sync.failed:
        reasons = "; ".join(f"{name}: {reason}" for name, reason in sync.failed)
        raise RuntimeError(f"could not sync the human-merge-only label: {reasons}")

    run(
        ["git", "fetch", "origin", config.base_branch],
        cwd=project_root,
        timeout=SLOW_GIT_TIMEOUT_S,
    )
    base_sha = run(
        ["git", "rev-parse", "--short", f"origin/{config.base_branch}"], cwd=project_root
    ).stdout.strip()
    branch = f"{branch_prefix}{base_sha}"
    task_id = f"evolve-{lane}-tmp"
    wt = worktree.create_detached(
        project_root, config.worktree_dir, task_id, f"origin/{config.base_branch}"
    )
    try:
        prompt = render_task(
            "evolve",
            lane=lane,
            survey=render_survey(rows),
            baseline=render_baseline(b),
            notes="\n".join(f"- {n}" for n in notes) if notes else "(none)",
        )
        runtime, request = role_request(
            config,
            "planner",
            prompt,
            wt,
            # may edit ONLY prompts/tasks/<lane>.md (enforced below, not by
            # this grant — see `worktree.changed_paths`); reads CI/PR
            # history for citations
            extra_allowed_tools=(
                "Edit",
                "Bash(gh run view:*)",
                "Bash(gh run list:*)",
                "Bash(gh pr view:*)",
                "Bash(gh pr list:*)",
            ),
        )
        result = run_with_fallback(runtime, request, on_event=log)
        if not result.ok:
            raise RuntimeError(f"Evolve run failed: {result.text}")

        try:
            verdict = parse_evolve(result.text)
        except ValueError as exc:
            raise RuntimeError(f"Evolve produced an unparseable verdict: {exc}") from exc
        if verdict is None:
            raise RuntimeError(f"Evolve produced no parseable verdict:\n{result.text[-500:]}")

        changed = worktree.changed_paths(wt)
        if changed and changed != [allowed]:
            raise RuntimeError(f"evolve touched disallowed path(s): {changed}")

        if isinstance(verdict, NoopVerdict):
            if changed:
                raise RuntimeError(f"no-op verdict but {allowed} was changed anyway")
            log(f"no-op for {lane!r}: {verdict.reason}")
            return []

        if not changed:
            raise RuntimeError(f"evolve proposed change(s) but {allowed} is unchanged")

        run(["git", "checkout", "-b", branch], cwd=wt)
        run(["git", "add", allowed], cwd=wt)
        worktree.commit(wt, f"prompts: evolve {lane}", paths=[allowed])
        run(["git", "push", "-u", "origin", branch], cwd=wt, timeout=SLOW_GIT_TIMEOUT_S)

        body = _pr_body(verdict, baseline_block=render_baseline(b), notes=notes)
        url = github.create_pr(
            wt,
            base=config.base_branch,
            title=f"prompts: evolve {lane}",
            body=body,
            draft=True,
            labels=("human-merge-only",),
        )
        log(f"opened draft PR: {url}")
        for c in verdict:
            log(f"{c.mode}: {c.summary}")
        return verdict
    finally:
        worktree.remove(project_root, config.worktree_dir, task_id, force=True, delete_branch=True)
