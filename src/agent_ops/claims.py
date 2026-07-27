"""The cross-machine "an agent is working on this right now" signal.

Two lanes implemented issue #116 in parallel — a hand-dispatched local agent and
the scheduled CI triage lane — because nothing either one could see said the
other had started. The local side's evidence (a worktree, `.agent-runs/`) is on
one machine; a fresh CI runner cannot read any of it. The labels the CI lane does
select on all describe an issue's *classification*, never its *occupancy*.

This module is that missing signal, as a label on the issue. Not a store: the
claim lives in GitHub with everything else (ADR 0003), and the one piece of
metadata a TTL needs — when the claim was applied — is read back from GitHub's
own issue-events timeline rather than written down anywhere. There is no file to
go stale and nothing to reconcile after a crash except the label itself.

Why a label and not something richer:

- An **assignee** is the closest semantic match and the CI lane already skips
  assigned issues, but the local lane runs under the operator's `gh` auth, so the
  only identity it can assign is the human's. That is a lie in the field a human
  reads to mean "mine", and assigning a bot needs an App identity this side does
  not have.
- A **draft PR** is visible everywhere and both lanes already skip issues with an
  open PR — but it cannot exist before there is a commit to push, and it puts a
  placeholder in the review queue. It stays what it already is: the guard that
  applies once work exists, not a claim on work that hasn't started.
- An **issue comment** could carry a payload, but the selector would have to read
  comment bodies, and every run would leave permanent thread noise.

Everything here is best-effort in the same sense `_record_halt` is: a failure to
apply or clear a claim logs and returns False, and never raises into a run. A
repo with no `origin` remote — a test or scratch checkout — has no shared issue
tracker to coordinate through at all, so claiming there is a no-op that says so.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_ops import github, runs, worktree
from agent_ops.utils import CommandError, run

CLAIM_LABEL = "agent:claimed"
CLAIM_LABEL_COLOR = "006b75"
CLAIM_LABEL_DESCRIPTION = "An agent is working on this right now; other lanes skip it"

# Checked in, unlike the runtimes' own `.claude/settings.local.json`: this is
# what lets a worktree checked out for `fix/issue-N` carry the hook before any
# agent-ops command has ever run there — the hand-started case issue #178
# exists for. `agent claim --auto` is what makes that safe to run unattended:
# no matching branch, no `gh`, no network all silently no-op, so a declined or
# missing hook is never worse than the manual-only path was.
CLAIM_SETTINGS_REL = Path(".claude") / "settings.json"
CLAIM_HOOK_COMMAND = "command -v agent >/dev/null 2>&1 && agent claim --auto || true"
CLAIM_HOOK_TIMEOUT_S = 30

# How long a claim is believed before it is treated as the residue of a run that
# died. The enforcing read is the CI lane's (prompts/orchestrator.md), because a
# claim left by a laptop that is now closed has to expire even though nothing
# local will ever run again to clear it.
#
# Eight hours, from both ends. Real runs finish inside the hour; the config's
# own worst case — planner, `max_attempts` implement attempts and a reviewer all
# running to `run_timeout_seconds`, with every gate at `gate_timeout_seconds` —
# stacks to something like six, so a healthy run does not reach this even
# pathologically. At the other end, a crash blocks the issue for at most two
# 4-hourly triage cycles.
#
# It errs toward expiring early on purpose. Trading duplicated work for a
# silently-blocked issue is the worse trade, and the cost of expiring under a
# live run is bounded and familiar: the CI lane may start a duplicate, which is
# exactly what it did before any of this existed. The cost of never expiring is
# an issue nothing will ever pick up and nobody is told about.
CLAIM_TTL_S = 8 * 3600.0

_LABELED_EVENT_JQ = (
    f'.[] | select(.event == "labeled" and .label.name == "{CLAIM_LABEL}") | .created_at'
)


def _quiet(_: str) -> None:
    """Default log sink — claiming is bookkeeping, not output."""


def claim(project_root: Path, issue: int, *, log: Callable[[str], None] = _quiet) -> bool:
    """Mark `issue` as occupied. Best-effort: returns False, never raises.

    The label is created first, with `--force` so it is create-or-update: a repo
    that was onboarded before this label existed would otherwise fail every
    claim on "label not found", which is the silent-blocking direction — the
    claim would never appear and the duplication it prevents would come back
    with nothing to show it had.
    """
    if github.remote_slug(project_root) is None:
        log(f"no `origin` remote — #{issue} was not claimed (nothing else can see this repo)")
        return False
    try:
        run(
            [
                "gh",
                "label",
                "create",
                CLAIM_LABEL,
                "--color",
                CLAIM_LABEL_COLOR,
                "--description",
                CLAIM_LABEL_DESCRIPTION,
                "--force",
            ],
            cwd=project_root,
            check=False,
        )
        proc = run(
            ["gh", "issue", "edit", str(issue), "--add-label", CLAIM_LABEL],
            cwd=project_root,
            check=False,
        )
    except (CommandError, OSError) as exc:
        # OSError as well as CommandError: `utils.run` raises FileNotFoundError
        # when `gh` is not on PATH, and a missing CLI must not crash a run any
        # more than a failed API call does.
        log(f"could not claim #{issue}: {exc}")
        return False
    if proc.returncode != 0:
        log(f"could not claim #{issue}: {github.why(proc.stderr, proc.stdout)}")
        return False
    return True


def release(project_root: Path, issue: int, *, log: Callable[[str], None] = _quiet) -> bool:
    """Clear `issue`'s claim. Best-effort in exactly the way `claim` is.

    Removing a label an issue does not carry succeeds as long as the label
    exists in the repo, so a release that runs twice — a run that reported for
    itself and then hit the session-end hook — is not an error.
    """
    if github.remote_slug(project_root) is None:
        return False
    try:
        proc = run(
            ["gh", "issue", "edit", str(issue), "--remove-label", CLAIM_LABEL],
            cwd=project_root,
            check=False,
        )
    except (CommandError, OSError) as exc:
        log(f"could not clear the claim on #{issue}: {exc}")
        return False
    if proc.returncode != 0:
        log(
            f"could not clear the claim on #{issue} (it may never have been claimed): "
            f"{github.why(proc.stderr, proc.stdout)}"
        )
        return False
    return True


def seed_claim_hook(project_root: Path, *, log: Callable[[str], None] = _quiet) -> Path | None:
    """Wire a `SessionStart` hook that runs `agent claim --auto`. Best-effort.

    Written by `agent init` into `.claude/settings.json` — the checked-in
    settings file, so it ships with the branch and is already in place by the
    time a hand-started worktree's session boots, the one case nothing else
    claims on behalf of.

    Merges into an existing file rather than replacing it, and never adds the
    same hook twice, the same way the runtimes' own stop-hook seeding does.
    Returns the file written, or None when it could not be (unreadable JSON,
    a non-object `hooks` or `hooks.SessionStart` key, an unwritable path).
    """
    path = project_root / CLAIM_SETTINGS_REL
    settings: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log(f"warning: could not read {path}, leaving it alone: {exc}")
            return None
        if not isinstance(loaded, dict):
            log(f"warning: {path} is not a JSON object, leaving it alone")
            return None
        settings = loaded

    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        log(f"warning: {path} has a non-object `hooks` key, leaving it alone")
        return None
    matchers = hooks.setdefault("SessionStart", [])
    if not isinstance(matchers, list):
        log(f"warning: {path} has a non-list `hooks.SessionStart` key, leaving it alone")
        return None

    already = any(
        isinstance(matcher, dict)
        and isinstance(matcher.get("hooks"), list)
        and any(
            isinstance(entry, dict) and entry.get("command") == CLAIM_HOOK_COMMAND
            for entry in matcher["hooks"]
        )
        for matcher in matchers
    )
    if not already:
        matchers.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": CLAIM_HOOK_COMMAND,
                        "timeout": CLAIM_HOOK_TIMEOUT_S,
                    }
                ]
            }
        )

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n")
    except OSError as exc:
        log(f"warning: could not seed the claim hook at {path}: {exc}")
        return None
    return path


@dataclass
class Claim:
    """One run's claim on one issue, so the release can be unconditional.

    A run has no exit where it returns with an agent still working — the process
    is on its way out either way — so `release()` belongs in a `finally` around
    the whole run rather than at each of the eight terminal paths, which is how
    they drift apart. `taken` is what keeps that `finally` from firing an API
    call for a claim that was never made (the already-has-an-open-PR bail-out,
    or a repo with no remote).
    """

    project_root: Path
    issue: int
    log: Callable[[str], None] = field(default=_quiet)
    taken: bool = False

    def take(self) -> bool:
        self.taken = claim(self.project_root, self.issue, log=self.log) or self.taken
        return self.taken

    def release(self) -> bool:
        if not self.taken:
            return False
        cleared = release(self.project_root, self.issue, log=self.log)
        self.taken = not cleared
        return cleared


def claimed_issues(project_root: Path) -> list[int]:
    """Every open issue currently carrying the claim label, ascending.

    Raises `CommandError` when `gh` cannot answer — unlike `claim`/`release`,
    this one is only ever called from a reporting path (`agent doctor`), where
    "could not check" is a thing to say rather than a run to protect.
    """
    proc = run(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--label",
            CLAIM_LABEL,
            "--json",
            "number",
            "--limit",
            "100",
        ],
        cwd=project_root,
    )
    return sorted(int(entry["number"]) for entry in json.loads(proc.stdout))


def claimed_at(project_root: Path, issue: int) -> float | None:
    """When this issue was last claimed, as epoch seconds, or None if unknowable.

    Read from GitHub's issue-events timeline rather than recorded anywhere: the
    claim's age is the one thing a TTL needs, and GitHub already stores it. That
    is what lets the label carry a TTL without the label carrying a payload —
    and it means a claim applied by another machine (or by hand) is dated as
    accurately as one this process applied.

    Events come back oldest-first, so the last matching line is the current
    claim: a re-claim after a release re-dates it, which is correct.
    """
    try:
        proc = run(
            [
                "gh",
                "api",
                f"repos/{{owner}}/{{repo}}/issues/{issue}/events",
                "--paginate",
                "--jq",
                _LABELED_EVENT_JQ,
            ],
            cwd=project_root,
            check=False,
        )
    except (CommandError, OSError):
        return None
    if proc.returncode != 0:
        return None
    stamps = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return _epoch(stamps[-1]) if stamps else None


def _epoch(stamp: str) -> float | None:
    """`2026-07-26T04:13:04Z` → epoch seconds; None for anything unparseable."""
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


@dataclass(frozen=True)
class StaleClaim:
    issue: int
    detail: str


@dataclass(frozen=True)
class ClaimAudit:
    """What `agent doctor` says about this repo's claims.

    Both halves matter, and they fail in opposite directions: `stale` is an
    issue no lane will touch because a dead run still holds it, `unclaimed` is
    an issue *this machine* is working on that the CI lane can still pick up —
    the original #131 collision, made visible.
    """

    stale: list[StaleClaim]
    unclaimed: list[int]
    claimed: list[int]


def audit(
    project_root: Path,
    *,
    now: float | None = None,
    ttl_s: float = CLAIM_TTL_S,
) -> ClaimAudit:
    """Reconcile this repo's claims against their age and this machine's runs.

    Two stale shapes, and the second exists because the first is slow:

    - **Past the TTL.** The only signal that works for a claim taken by a
      machine that is now switched off, and the one the CI lane enforces.
    - **Contradicted by a local outcome record.** A run that recorded a terminal
      outcome *after* the claim was applied and left the claim standing did not
      release it — a failed `gh` call at exactly the wrong moment. That is a
      stale claim the TTL would still hide for hours, so it is worth naming
      immediately. The timestamp comparison is what keeps it honest: an outcome
      record predating the claim belongs to an earlier cycle and says nothing
      about this one, and a claim whose date GitHub would not give up is never
      accused on this basis at all.

    Deliberately *not* treated as stale: a claim with no local run signal. This
    machine is not the only one that can claim, and "I can't see it" is not
    evidence a run is dead — the same reasoning `runs.wait_for_runs` applies to
    a degraded poll.

    Raises `CommandError` if `gh` or `git worktree list` cannot answer; the
    caller (`agent doctor`) reports that rather than guessing.
    """
    now = datetime.now().timestamp() if now is None else now
    claimed = claimed_issues(project_root)

    stale: list[StaleClaim] = []
    for issue in claimed:
        applied = claimed_at(project_root, issue)
        age = None if applied is None else now - applied
        if age is not None and age > ttl_s:
            stale.append(
                StaleClaim(
                    issue,
                    f"#{issue} has been claimed for {_fmt_age(age)}, past the "
                    f"{_fmt_age(ttl_s)} TTL — the run that claimed it never released it",
                )
            )
            continue
        outcome = runs.read_outcome(project_root, issue)
        if applied is None or outcome is None or outcome.finished_at is None:
            continue
        if outcome.finished_at > applied:
            stale.append(
                StaleClaim(
                    issue,
                    f"#{issue} is still claimed, but this machine recorded "
                    f"`{outcome.state}` for it after the claim was applied — the "
                    f"release did not happen",
                )
            )

    held = set(claimed)
    unclaimed = sorted(issue for issue in _local_task_issues(project_root) if issue not in held)
    return ClaimAudit(stale=stale, unclaimed=unclaimed, claimed=claimed)


def _local_task_issues(project_root: Path) -> set[int]:
    """Issue numbers this checkout has a `fix/issue-N` worktree for.

    The same branch convention `runs.discover_runs` derives from, read the same
    way — but without its `ps` scan or its `gh pr list`: the question here is
    only "is this machine holding a worktree for that issue", which the worktree
    list answers on its own.
    """
    issues: set[int] = set()
    for wt in worktree.list_worktrees(project_root):
        issue = runs.issue_from_branch(wt.branch)
        if issue is not None:
            issues.add(issue)
    return issues


def _fmt_age(seconds: float) -> str:
    """A duration a human scans rather than parses: `11h`, `45m`, `<1m`."""
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h"
    if seconds >= 60:
        return f"{int(seconds // 60)}m"
    return "<1m"
