"""Mirror CI agent-lane state into Orca as a read-only viewer.

Execution stays in CI (ADR 0003: GitHub is the database). This module only
*reads* GitHub through `gh` and reflects what it finds onto Orca worktree
cards, so lanes running on a GitHub Actions runner are reviewable in the app
— diff included — without a remote Orca host or a second set of credentials.

Nothing here writes to GitHub, and nothing here removes a card: a card can be
owned by a live local `agent implement` run, and a viewer must never fight the
thing it is watching.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_ops import orca, worktree
from agent_ops.config import load_project_config
from agent_ops.registry import RegistryConfig
from agent_ops.utils import CommandError, run
from agent_ops.workflows.implement import task_identifiers

READY_LABEL = "agent-ready"

# Branches the agent lanes push: `fix/issue-N` targets the working branch,
# `hotfix/issue-N` targets the stable one (README branch model).
_AGENT_BRANCH_RE = re.compile(r"^(fix|hotfix)/issue-(\d+)$")

# statusCheckRollup verdicts. Everything unlisted (SUCCESS, NEUTRAL, SKIPPED,
# ...) counts as passing: a viewer must not cry wolf over a skipped check.
_FAILING = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
_PENDING = {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "REQUESTED", "EXPECTED"}

_PR_FIELDS = "number,title,url,headRefName,baseRefName,isDraft,reviewDecision,statusCheckRollup"


@dataclass(frozen=True)
class Lane:
    """One unit of active agent work in a repo, as GitHub reports it.

    `branch` is None for lanes with nothing pushed yet (a queued issue); those
    are reported but never given a card, since there is no diff to show and a
    local run may already own the issue's card.
    """

    repo: str
    kind: str  # implement | hotfix | promote | queued
    label: str  # "PR #42" / "issue #7"
    branch: str | None
    comment: str  # what the card's comment should say
    status: str | None  # Orca workspace status id, None = leave the card's status alone
    card_name: str | None  # worktree directory name to use if a card must be created

    @property
    def ensure_card(self) -> bool:
        """Whether a missing card should be created (vs. adopted-if-present).

        Only issue lanes get a fresh checkout. A promotion PR's head is the
        long-lived working branch — mirroring that into a task worktree would
        fight the clone's own checkout for no review benefit.
        """
        return self.branch is not None and self.card_name is not None


def check_state(rollup: object) -> str:
    """Collapse a PR's statusCheckRollup into failing / pending / passing / none.

    Handles both shapes gh returns: CheckRun entries (`status` + `conclusion`)
    and commit StatusContext entries (`state`). Worst verdict wins, with
    failing ahead of pending — a red check is the thing worth surfacing even
    while other checks still run.
    """
    if not isinstance(rollup, list) or not rollup:
        return "none"
    states = {_entry_state(entry) for entry in rollup}
    for verdict in ("failing", "pending"):
        if verdict in states:
            return verdict
    return "passing"


def _entry_state(entry: object) -> str:
    if not isinstance(entry, dict):
        return "passing"
    status = str(entry.get("status") or "").upper()
    # A running CheckRun has no conclusion yet; a StatusContext has only state.
    verdict = str(entry.get("conclusion") or entry.get("state") or "").upper()
    if status in _PENDING or verdict in _PENDING:
        return "pending"
    if verdict in _FAILING:
        return "failing"
    return "passing"


def _pr_lane(repo: str, pr: dict[str, Any], stable_branch: str, working_branch: str) -> Lane | None:
    """Lane for one open PR, or None when the PR is not an agent lane."""
    head = str(pr.get("headRefName") or "")
    base = str(pr.get("baseRefName") or "")
    number = pr.get("number")
    checks = check_state(pr.get("statusCheckRollup"))
    review = str(pr.get("reviewDecision") or "").lower().replace("_", " ")
    label = f"PR #{number}"

    match = _AGENT_BRANCH_RE.fullmatch(head)
    if match is not None:
        prefix, issue = match.group(1), int(match.group(2))
        task_id, fix_branch = task_identifiers(issue)
        kind = "implement" if prefix == "fix" else "hotfix"
        detail = f"issue #{issue} → {base} · checks {checks}"
        return Lane(
            repo=repo,
            kind=kind,
            label=label,
            branch=head,
            comment=_comment(kind, label, detail, review, bool(pr.get("isDraft"))),
            status=_status(checks, bool(pr.get("isDraft"))),
            card_name=task_id if head == fix_branch else head.replace("/", "-"),
        )

    # Promotion PR: the working branch opened against the stable one. Only
    # humans merge it, so it is pure review surface — adopt a card, never mint.
    if head == working_branch and base == stable_branch and head != base:
        detail = f"{head} → {base} · checks {checks}"
        return Lane(
            repo=repo,
            kind="promote",
            label=label,
            branch=head,
            comment=_comment("promote", label, detail, review, bool(pr.get("isDraft"))),
            status=_status(checks, bool(pr.get("isDraft"))),
            card_name=None,
        )
    return None


def _comment(kind: str, label: str, detail: str, review: str, draft: bool) -> str:
    """Short one-liner for the card comment — Orca shows it inline, so keep it terse."""
    parts = [f"{kind} lane · {label}", detail]
    if draft:
        parts.append("draft")
    if review:
        parts.append(f"review {review}")
    return " · ".join(parts)


def _status(checks: str, draft: bool) -> str:
    """Green and ready for eyes is `in-review`; anything else is still moving."""
    if draft or checks in ("failing", "pending"):
        return orca.STATUS_IN_PROGRESS
    return orca.STATUS_IN_REVIEW


def discover_lanes(
    repo: str,
    prs: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    *,
    working_branch: str = "main",
    stable_branch: str = "main",
) -> list[Lane]:
    """Active agent lanes for one repo, from its open PRs and open issues.

    Pure: callers fetch, this decides. Queued lanes are the agent-ready issues
    with no agent PR yet — reported so the viewer shows the whole pipeline, but
    card-less by design (see `Lane.branch`).
    """
    lanes = [
        lane
        for pr in prs
        if (lane := _pr_lane(repo, pr, stable_branch, working_branch)) is not None
    ]
    covered = {lane.branch for lane in lanes}
    for issue in issues:
        number = issue.get("number")
        if not isinstance(number, int):
            continue
        if task_identifiers(number)[1] in covered:
            continue  # already in flight as a PR lane
        lanes.append(
            Lane(
                repo=repo,
                kind="queued",
                label=f"issue #{number}",
                branch=None,
                comment=f"queued lane · issue #{number} · {READY_LABEL}, no PR yet",
                status=orca.STATUS_TODO,
                card_name=None,
            )
        )
    return lanes


def open_prs(repo: str) -> list[dict[str, Any]]:
    proc = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "50",
            "--json",
            _PR_FIELDS,
        ]
    )
    return list(json.loads(proc.stdout))


def ready_issues(repo: str) -> list[dict[str, Any]]:
    proc = run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--label",
            READY_LABEL,
            "--limit",
            "50",
            "--json",
            "number,title",
        ]
    )
    return list(json.loads(proc.stdout))


@dataclass(frozen=True)
class CardPlan:
    """What one lane needs done to its card, decided before anything is written."""

    lane: Lane
    text: str  # what to log for this lane
    path: Path | None = None  # card to write; None means there is nothing to write
    fresh: bool = False  # the checkout was just made, so Orca may not see it yet


def plan_card(
    lane: Lane, repo: orca.Repo, cards: dict[str, orca.Card], worktree_dir: str
) -> CardPlan:
    """Decide the lane's card, creating the checkout it needs if there is none.

    Idempotency lives here: cards are keyed by branch, an existing card is
    adopted rather than duplicated, and a card that already says the right
    thing is left alone entirely.
    """
    if lane.branch is None:
        return CardPlan(lane, f"{lane.comment} — no card (nothing pushed yet)")

    card = cards.get(lane.branch)
    if card is not None:
        if card.comment == lane.comment and card.workspace_status == lane.status:
            return CardPlan(lane, f"already current ({lane.status})")
        return CardPlan(lane, "card updated", card.path)

    if not lane.ensure_card or lane.card_name is None:
        return CardPlan(lane, f"no card tracks {lane.branch} — reported only")
    try:
        path = worktree.create_tracking(repo.path, worktree_dir, lane.card_name, lane.branch)
    except (CommandError, FileExistsError) as exc:
        return CardPlan(lane, f"could not mirror {lane.branch}: {exc}")
    return CardPlan(lane, "card created", path, fresh=True)


def write_card(plan: CardPlan) -> str:
    """Push one planned card to Orca; returns the line to log for it."""
    lane = plan.lane
    if plan.path is None:
        return _line(lane, plan.text)
    if not orca.report(plan.path, comment=lane.comment, status=lane.status):
        return _line(lane, f"{plan.path} not indexed by Orca yet — re-run the sync")
    return _line(lane, f"{plan.text} → {lane.status}  ({lane.comment})")


def _line(lane: Lane, text: str) -> str:
    return f"{lane.label:>10}  {lane.kind:<9} {text}"


def sync_orca(config: RegistryConfig, log: Callable[[str], None] = print) -> None:
    """Reflect every registered repo's active agent lanes onto Orca cards.

    Read-only towards GitHub, and re-runnable: repeated runs converge on the
    same set of cards instead of accumulating duplicates.
    """
    if not orca.available():
        log(
            "Orca is not running (no `orca` CLI on PATH, or `orca status` failed) — "
            "nothing synced. Start the Orca app and re-run `agent status --sync-orca`."
        )
        return

    known = {repo.slug: repo for repo in orca.repos() if repo.slug is not None}
    for slug in config.repos:
        log(f"\n\033[1m{slug}\033[0m")
        target = known.get(slug)
        if target is None:
            log("  not registered in Orca — skipped (add its clone with `orca repo add <path>`)")
            continue
        try:
            # The clone's own config decides which branches count as the
            # working/stable pair; defaults (main/main) simply detect no
            # promotion lane, which is the safe reading.
            project = load_project_config(target.path)
            lanes = discover_lanes(
                slug,
                open_prs(slug),
                ready_issues(slug),
                working_branch=project.base_branch,
                stable_branch=project.merge.stable_branch,
            )
        except (CommandError, ValueError) as exc:
            log(f"  skipped — could not read lane state: {exc}")
            continue
        if not lanes:
            log("  no active agent lanes")
            continue
        cards = {card.branch: card for card in orca.cards(target.id) if card.branch}
        # Check every lane out first, then wait once for Orca's rescan to find
        # the new ones, then write. Per-card waiting would pay that latency
        # again for every fresh checkout in the batch.
        plans = [plan_card(lane, target, cards, project.worktree_dir) for lane in lanes]
        fresh = [plan.path for plan in plans if plan.fresh and plan.path is not None]
        if fresh:
            log(f"  waiting for Orca to index {len(fresh)} new worktree(s)…")
            orca.await_indexed(fresh)
        for plan in plans:
            log("  " + write_card(plan))
