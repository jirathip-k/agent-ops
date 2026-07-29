from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from fnmatch import fnmatch, fnmatchcase
from pathlib import Path
from typing import Any, Literal

from agent_ops import github
from agent_ops.config import ProjectConfig, load_project_config
from agent_ops.runs import Run, discover_runs, issue_from_branch
from agent_ops.utils import DEFAULT_TIMEOUT_S, SLOW_GIT_TIMEOUT_S, CommandError, run


def _is_test_file(path: str, patterns: list[str]) -> bool:
    """Whether `path` matches one of the test-file patterns.

    Matched with `fnmatchcase`, not `fnmatch`, so case-sensitivity is
    enforced on every platform. Plain `fnmatch` runs both operands through
    `os.path.normcase` first — a no-op on POSIX but `str.lower()` on
    Windows — which would silently fail OPEN there: it turns the CamelCase
    Swift pattern `*Tests/*` into `*tests/*`, which then substring-matches
    production directories like `src/contests/model.py` or
    `app/protests/view.py` (fnmatch's `*` spans `/`, so there is no word
    boundary) and wrongly exempts them from the size caps. `blocked_paths`
    lowercases too, but over-matching there only blocks *more* paths (fails
    safe); over-matching here would exempt more paths from the caps (fails
    open), so the two must not share the same rule.

    Patterns containing `[!/]` (the `test_*.py` family) are matched against
    the basename, not the full path. `[!/]` only protects the ONE character
    position right after it — fnmatch's `*` still spans `/` on either side —
    so `test_[!/]*.py` matched against the full path would match
    `test_data/schema.py` (and `src/test_data/schema.py`) as if they were
    `test_<name>.py`, when both are production files that merely start with
    `test_`. Matching the basename makes the intended anchor-to-filename
    behavior exact, for both `test_[!/]*.py` and its `*/`-prefixed twin, and
    changes nothing for any other pattern in the default list.
    """
    name = path.rsplit("/", 1)[-1]
    return any(fnmatchcase(name if "[!/]" in pattern else path, pattern) for pattern in patterns)


def evaluate_merge(pr: dict[str, Any], config: ProjectConfig) -> list[str]:
    """Return the list of rule violations blocking an agent merge (empty = mergeable).

    Two supported shapes, distinguished by whether `merge.stable_branch` is a
    separate branch from `base_branch`:
    - Two-branch (promotion) model: agents merge into `base_branch` (e.g.
      `staging`); `merge.stable_branch` (e.g. `main`) is always human-only —
      `agent promote` opens that verification PR, never an agent merge.
    - Single-branch model: `base_branch == merge.stable_branch` (both `main`,
      the common default), so agents merge straight into it; there is no
      separate stable branch to carve out.

    `pr["labels"]` carrying one of `config.merge.blocked_labels` (e.g.
    `human-merge-only`) is a violation like any other here — `--override` can
    bypass it the same way it bypasses a size cap or a blocked path, since
    this rail is for agents, not for the human operator who applied the
    label in the first place.
    """
    violations: list[str] = []
    single_branch = config.base_branch == config.merge.stable_branch

    if pr["baseRefName"] != config.base_branch:
        human_only_note = (
            ""
            if single_branch
            else f" (stable branch {config.merge.stable_branch!r} is human-only)"
        )
        violations.append(
            f"base is {pr['baseRefName']!r}, agents may only merge into "
            f"{config.base_branch!r}{human_only_note}"
        )
    if not single_branch and pr["baseRefName"] == config.merge.stable_branch:
        violations.append(f"target {config.merge.stable_branch!r} is the stable branch — never")

    files = pr.get("files", [])
    changed_lines = sum(f["additions"] + f["deletions"] for f in files)

    # Production caps: test files are close to zero review risk, so they are
    # excluded from what counts against max_changed_lines/max_changed_files.
    # A test-only PR is NOT specially fallen back to raw counts here — it is
    # governed solely by the backstop below (owner-approved design, issue
    # #136: an earlier fallback applied the strictest cap to the
    # lowest-risk PRs, since one production line would exempt a PR from it).
    prod_files = [f for f in files if not _is_test_file(f["path"], config.merge.test_paths)]
    effective_lines = sum(f["additions"] + f["deletions"] for f in prod_files)
    effective_files = len(prod_files)

    if effective_lines > config.merge.max_changed_lines:
        detail = (
            f" ({effective_lines} effective, tests excluded)"
            if effective_lines != changed_lines
            else ""
        )
        violations.append(
            f"{changed_lines} changed lines{detail} > cap {config.merge.max_changed_lines}"
        )
    if effective_files > config.merge.max_changed_files:
        detail = (
            f" ({effective_files} effective, tests excluded)"
            if effective_files != len(files)
            else ""
        )
        violations.append(
            f"{len(files)} changed files{detail} > cap {config.merge.max_changed_files}"
        )

    # Backstop: bounds the RAW totals (tests included) so excluding test
    # lines/files from the caps above doesn't leave a mixed (or test-only) PR
    # with no ceiling at all.
    total_line_cap = config.merge.max_changed_lines * config.merge.total_cap_ratio
    if changed_lines > total_line_cap:
        violations.append(
            f"{changed_lines} total changed lines (including tests) > backstop cap {total_line_cap}"
        )
    total_file_cap = config.merge.max_changed_files * config.merge.total_cap_ratio
    if len(files) > total_file_cap:
        violations.append(
            f"{len(files)} total changed files (including tests) > backstop cap {total_file_cap}"
        )

    for f in files:
        for pattern in config.merge.blocked_paths:
            # case-insensitive: useAuth.ts must match *auth*
            if fnmatch(f["path"].lower(), pattern.lower()):
                violations.append(f"blocked path: {f['path']} (matches {pattern!r})")
                break

    label_names = {label["name"] for label in pr.get("labels", [])}
    for blocked in config.merge.blocked_labels:
        if blocked in label_names:
            violations.append(f"carries blocked label: {blocked!r}")
    return violations


def _runs_in_flight(project_root: Path, log: Callable[[str], None]) -> list[Run] | None:
    """Locally-running agent runs, or `None` if that can't be determined.

    `None` (unknown) is distinct from `[]` (checked, nothing running) — same
    fail-open contract as `status.local_deployed_lanes` and
    `stubs.caller_workflows`: a caller must never read "could not tell" as
    "nothing running". `discover_runs` degrades most of its own signals
    internally and reports that via its `trustworthy` flag, but leaves
    `.agent-runs/`'s own directory listing unguarded, so an unreadable
    registry is caught here too.
    """
    try:
        found, trustworthy = discover_runs(project_root, log=log)
    except (CommandError, OSError) as exc:
        log(f"warning: could not read local run state ({exc}) — proceeding")
        return None
    # A degraded poll only ever under-reports runs (misreports them as
    # stopped), never over-reports them (see runs.discover_runs) — so a
    # `running` row is positive, non-degradable evidence regardless of
    # `trustworthy`, and discarding it here would throw away exactly the
    # signal this check exists to act on. Only the absence of a `running`
    # row is ambiguous under `trustworthy=False`: that's "unknown", not
    # "nothing running".
    running = [r for r in found if r.state == "running"]
    if running:
        return running
    return None if not trustworthy else []


# Substrings (matched case-insensitively against a failed `gh pr merge`'s
# combined stdout+stderr) that identify GitHub's "the branch isn't actually
# up to date" rejection specifically — the one failure mode `run_merge_batch`
# retries (issue #277), as distinct from every other reason `gh pr merge` can
# fail (permissions, required review, etc.), which must keep crashing exactly
# as before.
_STALE_BRANCH_MARKERS = (
    "not up to date with the base branch",
    "not up-to-date with the base branch",
)


class MergeNotUpToDateError(CommandError):
    """`gh pr merge` rejected the merge because the branch isn't up to date.

    Raised instead of a plain `CommandError` only when the failure message
    matches `_STALE_BRANCH_MARKERS` (issue #277): GitHub's `mergeStateStatus`
    is eventually consistent, so a PR read as `CLEAN` right after an earlier
    batch merge can still be rejected by the authoritative `gh pr merge` call
    a moment later. A subclass of `CommandError`, so any caller not
    specifically catching this (a lone `agent merge`, or anything calling
    `run_merge` directly) behaves exactly as it did before — the same
    uncaught crash. Only `run_merge_batch` catches it, to re-fetch the PR
    fresh and retry the merge once after forcing an update.
    """


def _fetch_pr(project_root: Path, pr_number: int) -> dict[str, Any]:
    proc = run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            # `labels` feeds merge.blocked_labels — requested here rather than
            # only in run_merge so `agent merge --check` sees them too. The CI
            # lane reaches the rules through --check, so a label enforced only
            # on the local path would be no containment at all.
            # `mergeStateStatus` feeds run_merge_batch's `_update_and_wait`
            # (issue #272): DIRTY vs BEHIND is the only signal that decides
            # whether a batched PR can be brought up to date at all.
            "baseRefName,headRefName,title,url,files,state,labels,mergeStateStatus",
        ],
        cwd=project_root,
    )
    return json.loads(proc.stdout)  # type: ignore[no-any-return]


def run_merge_check(
    project_root: Path,
    pr_number: int,
    *,
    log: Callable[[str], None] = print,
) -> list[str]:
    """Report merge-rule violations for a PR without merging it.

    Same rules `run_merge` enforces (`evaluate_merge`), exposed as a
    check-only entry point so the CI lane can judge caps by the same code
    the local lane merges with, instead of re-deriving the numbers in prompt
    prose (issue #150). A non-OPEN PR counts as blocking rather than clean.
    """
    config = load_project_config(project_root)
    pr = _fetch_pr(project_root, pr_number)
    if pr["state"] != "OPEN":
        violations = [f"PR #{pr_number} is {pr['state']} — nothing to merge"]
        log(violations[0])
        return violations

    violations = evaluate_merge(pr, config)
    if violations:
        for v in violations:
            log(f"blocked: {v}")
    else:
        log(f"PR #{pr_number} has no merge-rule violations")
    return violations


def run_merge(
    project_root: Path,
    pr_number: int,
    *,
    override: bool = False,
    force: bool = False,
    confirm: Callable[[str], bool] | None = None,
    log: Callable[[str], None] = print,
) -> bool:
    """Squash-merge a PR into the working branch if every rule passes.

    Rules: base must be the working branch, CI green (missing checks warn),
    diff within caps, no blocked paths, no blocked label. `override=True`
    merges anyway but logs every overridden rule — that is a human decision,
    never automate it.

    Runs elsewhere in the same repo are checked just before the merge itself
    (issue #258): merging now stales every one of their bases, which review
    only catches after the implementer already ran. The PR's own run (its
    headRefName's issue) is excluded from that count — it is the run that
    just produced this PR, not one this merge would stale, and it may still
    show as `running` if its process hasn't fully exited yet. `force=True`
    skips the prompt; otherwise `confirm` (when given, e.g. an interactive
    human) is asked, and a non-interactive caller (`confirm=None`) is refused
    with instructions to pass `--force`. This is advisory only — it never
    touches `evaluate_merge`'s violations, and in CI it is a no-op because no
    local run signals exist.
    """
    config = load_project_config(project_root)
    pr = _fetch_pr(project_root, pr_number)
    if pr["state"] != "OPEN":
        log(f"PR #{pr_number} is {pr['state']} — nothing to merge")
        return False

    checks = run(["gh", "pr", "checks", str(pr_number)], cwd=project_root, check=False)
    if checks.returncode != 0:
        if "no checks reported" in (checks.stderr + checks.stdout):
            log("warning: no CI checks on this repo — merging on local gates alone")
        else:
            log(f"CI checks are not green:\n{checks.stdout.strip()}")
            if not override:
                return False
            log("OVERRIDE: merging despite non-green checks")

    violations = evaluate_merge(pr, config)
    if violations:
        for v in violations:
            log(f"blocked: {v}")
        if not override:
            log(f"PR #{pr_number} NOT merged. Re-run with --override to force (human call).")
            return False
        log(f"OVERRIDE: merging despite {len(violations)} rule violation(s)")

    if not force:
        own = issue_from_branch(pr["headRefName"])
        in_flight = _runs_in_flight(project_root, log)
        if in_flight:
            in_flight = [r for r in in_flight if r.issue != own]
        if in_flight:
            log(
                f"{len(in_flight)} run(s) in flight — merging now stales each one's base, "
                "costing it a full cycle:"
            )
            for r in in_flight:
                log(f"  #{r.issue}  {r.detail}")
            log(
                "the cost is per merge, not per PR — batch any other PRs you're about to "
                "merge into this same round to pay it once"
            )
            if confirm is None:
                log(f"PR #{pr_number} NOT merged. Re-run with --force to merge anyway.")
                return False
            if not confirm(f"merge PR #{pr_number} anyway?"):
                log(f"PR #{pr_number} NOT merged.")
                return False

    # no --delete-branch: it also deletes the LOCAL branch, which fails (and
    # taints the exit code) while the task worktree still holds it. Delete
    # only the remote branch; locals are cleaned with the worktree.
    #
    # check=False here (issue #277): a rejection because the branch isn't
    # actually up to date (GitHub's mergeStateStatus is eventually
    # consistent, so a batch's next PR can read stale CLEAN) needs to be
    # distinguishable from every other merge failure, so `run_merge_batch`
    # can retry only that one reason. Every other non-zero exit raises the
    # same `CommandError`, with a byte-identical message to what `check=True`
    # would have raised — a lone `agent merge` crashes exactly as before.
    merge_cmd = ["gh", "pr", "merge", str(pr_number), "--squash"]
    merge_proc = run(merge_cmd, cwd=project_root, check=False)
    if merge_proc.returncode != 0:
        message = (
            f"`{' '.join(merge_cmd)}` failed with exit code {merge_proc.returncode}:\n"
            f"{merge_proc.stderr.strip() or merge_proc.stdout.strip()}"
        )
        combined = (merge_proc.stdout + merge_proc.stderr).lower()
        if any(marker in combined for marker in _STALE_BRANCH_MARKERS):
            raise MergeNotUpToDateError(message)
        raise CommandError(message)
    run(
        ["git", "push", "origin", "--delete", pr["headRefName"]],
        cwd=project_root,
        check=False,
    )
    log(f"merged PR #{pr_number} ({pr['title']}) into {pr['baseRefName']}")
    return True


# `bucket` values `gh pr checks --json` reports. "skipping" (a required check
# marked skipped, e.g. a path-filtered workflow) counts toward "passing" —
# GitHub itself lets a skipped required check merge, so a batch that stopped
# on it would be stricter than GitHub's own gate. "cancel" counts as failing:
# a cancelled required check never produced a passing result and is not
# going to on its own.
_PASSING_BUCKETS = {"pass", "skipping"}
_FAILING_BUCKETS = {"fail", "cancel"}


def _wait_for_required_checks(
    project_root: Path,
    pr_number: int,
    *,
    timeout_s: float,
    interval_s: float,
    log: Callable[[str], None],
) -> Literal["passing", "failing", "timeout", "none"]:
    """Poll `gh pr checks <n> --required` until every required check settles.

    Mirrors `runs.wait_for_runs`'s poll cadence: module-level `time.sleep`/
    `time.monotonic`, so tests can monkeypatch the clock instead of actually
    waiting. "none" (no required checks configured on this repo, or the repo
    has no branch protection at all) returns immediately — there is nothing
    to wait on, and `run_merge`'s own `gh pr checks` call already tolerates
    the same "no checks" shape.

    One poll always happens before the deadline is checked, so an
    already-settled PR returns immediately without sleeping.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        proc = run(
            ["gh", "pr", "checks", str(pr_number), "--required", "--json", "name,bucket,link"],
            cwd=project_root,
            check=False,
            timeout=DEFAULT_TIMEOUT_S,
        )
        if "no required checks" in (proc.stdout + proc.stderr).lower():
            return "none"
        try:
            checks = json.loads(proc.stdout) if proc.stdout.strip() else []
        except json.JSONDecodeError:
            checks = []
        if not checks:
            return "none"

        buckets = {c.get("bucket") for c in checks}
        if buckets & _FAILING_BUCKETS:
            failing = [c["name"] for c in checks if c.get("bucket") in _FAILING_BUCKETS]
            log(f"PR #{pr_number}: required check(s) failing: {', '.join(failing)}")
            return "failing"
        if buckets <= _PASSING_BUCKETS:
            return "passing"

        if time.monotonic() >= deadline:
            pending = [c["name"] for c in checks if c.get("bucket") not in _PASSING_BUCKETS]
            log(
                f"PR #{pr_number}: required check(s) still pending after {timeout_s:g}s: "
                f"{', '.join(pending)}"
            )
            return "timeout"
        time.sleep(interval_s)


def _update_and_wait(
    project_root: Path,
    pr_number: int,
    pr: dict[str, Any],
    *,
    timeout_s: float,
    interval_s: float,
    log: Callable[[str], None],
    force_update: bool = False,
) -> Literal["clean", "dirty", "failing", "timeout", "none"]:
    """Bring PR #`pr_number` up to date with its base, then wait for its
    required checks — the update→wait half of `agent merge --batch`.

    `mergeStateStatus == "DIRTY"` (real merge conflicts) refuses immediately:
    no `gh pr update-branch` call is made and nothing is force-pushed. GitHub's
    update-branch endpoint can only fast-forward-merge a branch that is
    cleanly behind; asking it to do anything with a conflicted branch is not
    a thing this command is ever allowed to attempt (a human resolves
    conflicts, never this batch). This check runs first and unconditionally —
    `force_update=True` never skips or weakens it.

    `"BEHIND"` runs `gh pr update-branch` once, then re-fetches
    `mergeStateStatus` fresh on every poll (never caching the rollup this
    function was called with) until it leaves BEHIND/UNKNOWN or `timeout_s`
    elapses. A PR that turns out DIRTY only once the update lands (the
    update itself surfaces a conflict) is caught here too and refused the
    same way.

    `force_update=True` (issue #277) runs that same update+poll even when
    `pr["mergeStateStatus"]` reads `CLEAN` — used for the one-shot retry
    after `gh pr merge` rejects a batched PR as not up to date despite a
    freshly-fetched `CLEAN` read: GitHub's `mergeStateStatus` is eventually
    consistent, so the authoritative `gh pr merge` rejection can lag behind
    what the rollup reports. `pr` must be a *fresh* re-fetch in that case,
    never the possibly-stale dict from before the rejection.
    """
    status = pr.get("mergeStateStatus")
    if status == "DIRTY":
        log(f"PR #{pr_number} is DIRTY (merge conflicts) — not updating, not merging")
        return "dirty"

    if status == "BEHIND" or force_update:
        if status == "BEHIND":
            log(f"PR #{pr_number} is behind its base — running gh pr update-branch")
        else:
            log(
                f"PR #{pr_number}: forcing gh pr update-branch after a stale "
                "mergeStateStatus read caused a merge rejection"
            )
        run(
            ["gh", "pr", "update-branch", str(pr_number)],
            cwd=project_root,
            timeout=DEFAULT_TIMEOUT_S,
        )
        deadline = time.monotonic() + timeout_s
        while True:
            proc = run(
                ["gh", "pr", "view", str(pr_number), "--json", "mergeStateStatus"],
                cwd=project_root,
                timeout=DEFAULT_TIMEOUT_S,
            )
            status = json.loads(proc.stdout)["mergeStateStatus"]
            if status == "DIRTY":
                log(f"PR #{pr_number} became DIRTY while updating — not merging")
                return "dirty"
            if status not in ("BEHIND", "UNKNOWN"):
                break
            if time.monotonic() >= deadline:
                log(f"PR #{pr_number} did not leave {status} within {timeout_s:g}s of updating")
                return "timeout"
            time.sleep(interval_s)

    verdict = _wait_for_required_checks(
        project_root, pr_number, timeout_s=timeout_s, interval_s=interval_s, log=log
    )
    if verdict in ("failing", "timeout"):
        return verdict
    return "clean"


def _stop_batch_if_terminal(
    verdict: Literal["clean", "dirty", "failing", "timeout", "none"],
    pr_number: int,
    not_attempted: str,
    log: Callable[[str], None],
) -> bool:
    """Log why the batch must stop for a terminal `_update_and_wait` verdict.

    "clean"/"none" are not terminal — they return `False` so the caller
    proceeds to merge. Factored out (issue #277) so the retry path added to
    `run_merge_batch` for a stale-mergeStateStatus merge rejection routes
    through the exact same dirty/failing/timeout handling as the primary
    pass, rather than a second copy of these branches that could drift from
    it.
    """
    if verdict == "dirty":
        log(f"PR #{pr_number} has merge conflicts — batch stopped, nothing auto-resolved")
    elif verdict == "failing":
        log(f"PR #{pr_number}'s required checks are failing — batch stopped")
    elif verdict == "timeout":
        log(f"PR #{pr_number}'s required checks did not settle in time — batch stopped")
    else:
        return False
    if not_attempted:
        log(not_attempted)
    return True


def run_merge_batch(
    project_root: Path,
    pr_numbers: list[int],
    *,
    override: bool = False,
    force: bool = False,
    confirm: Callable[[str], bool] | None = None,
    log: Callable[[str], None] = print,
) -> bool:
    """Update → wait for required checks → merge each PR in `pr_numbers`, in order.

    Stands in for a GitHub merge queue, which isn't available on a
    personal-account repo: `required_status_checks.strict=true` on the base
    branch means every merge marks every *other* open PR BEHIND, so merging a
    round of PRs one `agent merge` at a time re-stales the rest on every
    single merge. Routing a whole round through one call instead means each
    PR is only ever updated against the tree the *previous* PR in this same
    batch just produced.

    Sequential only, deliberately: unlike `review --jobs`, there is no
    parallelism knob here and never should be — a PR later in the list is
    tested against the result of the PR merged just before it, so merging two
    at once would test one of them against a base that's already stale by the
    time it lands.

    Stops (never skips) on the first PR that is DIRTY, has failing required
    checks, or times out waiting on them — and reports every PR after it as
    not attempted, rather than silently leaving them for a re-run to
    rediscover. A non-OPEN PR (closed/merged out from under the batch between
    scheduling and running) stops it the same way.

    The in-flight-runs guard (issue #265) is inherited for free: this
    function never checks for in-flight runs itself, and instead lets the
    unmodified `run_merge` call below do that check as it always has. If a
    run is in flight, `run_merge`'s own guard refuses the very first PR in
    the batch (unless `force`/`confirm` clears it, exactly as for a lone
    `agent merge`), so the batch reports that refusal and stops before
    merging anything — there is no second copy of that guard to keep in sync
    with the original.

    `mergeStateStatus` is eventually consistent (issue #277): the PR just
    after one this batch merged can be fetched fresh and still read stale
    `CLEAN`, even though GitHub's authoritative merge check will reject it as
    not actually up to date. `run_merge` raises `MergeNotUpToDateError` for
    exactly that rejection; this function catches it (once per PR) and
    re-fetches the PR fresh, forces an update via
    `_update_and_wait(force_update=True)`, and retries the merge once. A
    second `MergeNotUpToDateError` for the same PR stops the batch instead of
    looping — bounded to exactly one retry. Any other `CommandError` from
    `run_merge` (a different failure reason) is not caught here and
    propagates uncaught, exactly as before this retry existed.
    """
    config = load_project_config(project_root)
    for i, pr_number in enumerate(pr_numbers):
        remaining = pr_numbers[i + 1 :]
        not_attempted = (
            f"not attempted: {', '.join(f'#{n}' for n in remaining)}" if remaining else ""
        )

        pr = _fetch_pr(project_root, pr_number)
        if pr["state"] != "OPEN":
            log(f"PR #{pr_number} is {pr['state']} — nothing to merge, batch stopped")
            if not_attempted:
                log(not_attempted)
            return False

        verdict = _update_and_wait(
            project_root,
            pr_number,
            pr,
            timeout_s=config.merge.batch_check_timeout_s,
            interval_s=config.merge.batch_poll_interval_s,
            log=log,
        )
        if _stop_batch_if_terminal(verdict, pr_number, not_attempted, log):
            return False

        try:
            ok = run_merge(
                project_root, pr_number, override=override, force=force, confirm=confirm, log=log
            )
        except MergeNotUpToDateError:
            log(
                f"PR #{pr_number}: gh pr merge rejected it as not up to date with the base — "
                "mergeStateStatus was read stale before this retry; re-fetching the PR fresh, "
                "forcing gh pr update-branch, and retrying the merge once"
            )
            fresh_pr = _fetch_pr(project_root, pr_number)
            retry_verdict = _update_and_wait(
                project_root,
                pr_number,
                fresh_pr,
                timeout_s=config.merge.batch_check_timeout_s,
                interval_s=config.merge.batch_poll_interval_s,
                log=log,
                force_update=True,
            )
            if _stop_batch_if_terminal(retry_verdict, pr_number, not_attempted, log):
                return False
            try:
                ok = run_merge(
                    project_root,
                    pr_number,
                    override=override,
                    force=force,
                    confirm=confirm,
                    log=log,
                )
            except MergeNotUpToDateError:
                log(f"PR #{pr_number} failed to merge twice for the same reason — batch stopped")
                if not_attempted:
                    log(not_attempted)
                return False

        if not ok:
            log(f"PR #{pr_number} was not merged — batch stopped")
            if not_attempted:
                log(not_attempted)
            return False
        log(f"batch: merged {i + 1}/{len(pr_numbers)} ({', '.join(f'#{n}' for n in pr_numbers)})")

    log(f"batch complete: merged {len(pr_numbers)} PR(s)")
    return True


def batch_candidates(project_root: Path, *, log: Callable[[str], None] = print) -> list[int]:
    """Open PR numbers with no `evaluate_merge` violations and not DIRTY, ascending.

    Backs `agent merge --batch --all-clean`: builds the batch by filtering
    down the candidate set up front, rather than including every open PR and
    letting the batch itself stop at the first one that isn't clean. Those are
    different behaviours — `--all-clean` is meant to land everything that
    *is* mergeable in one round, not to give up the moment it meets a PR
    (however far down the list) that carries a blocked label or a conflict.
    """
    config = load_project_config(project_root)
    clean: list[int] = []
    for number in github.open_pr_numbers(config.base_branch, cwd=project_root):
        pr = _fetch_pr(project_root, number)
        if pr["state"] != "OPEN":
            continue
        if pr.get("mergeStateStatus") == "DIRTY":
            log(f"PR #{number} is DIRTY — excluded from --all-clean batch")
            continue
        violations = evaluate_merge(pr, config)
        if violations:
            log(f"PR #{number} has rule violation(s) — excluded from --all-clean batch")
            continue
        clean.append(number)
    return clean


def closable_issue_refs(commit_subjects: list[str], open_issues: set[int]) -> list[int]:
    """Issue numbers the promotion PR should auto-close.

    A subject's trailing "(#N)" refs name the issue that commit fixes (our
    commit convention) — plural because GitHub squash merges append their own
    "(#PR)" after the issue ref, as in "fix: thing (#111) (#116)". "(#N)"
    anywhere else — "part of #N", "PR #N: …" — is only a reference. Filtering
    against the repo's open issues drops PR numbers and already-closed issues.
    """
    refs: set[int] = set()
    for subject in commit_subjects:
        tail = re.search(r"((?:\s*\(#\d+\))+)$", subject)
        if tail:
            refs.update(int(n) for n in re.findall(r"#(\d+)", tail.group(1)))
    return sorted(refs & open_issues)


def run_promote(project_root: Path, *, log: Callable[[str], None] = print) -> str:
    """Open (or report) the human-verification PR: working branch → stable branch.

    Never merges — promotion into the stable branch is always the human's click.
    """
    config = load_project_config(project_root)
    working, stable = config.base_branch, config.merge.stable_branch
    if working == stable:
        # Single-branch model (see evaluate_merge): there is no separate
        # stable branch to promote into, so there is nothing for this
        # command to do — agents already merge straight into `working`.
        raise CommandError(
            f"base_branch and merge.stable_branch are both {stable!r} — "
            "configure base_branch: staging to use the promotion flow"
        )

    run(["git", "fetch", "origin", working, stable], cwd=project_root, timeout=SLOW_GIT_TIMEOUT_S)
    commits = run(
        ["git", "log", f"origin/{stable}..origin/{working}", "--pretty=%s"],
        cwd=project_root,
    ).stdout.strip()
    if not commits:
        log(f"{working} has nothing new for {stable} — no promotion needed")
        return ""

    existing = run(
        ["gh", "pr", "list", "--base", stable, "--head", working, "--json", "url"],
        cwd=project_root,
    )
    urls = json.loads(existing.stdout)
    if urls:
        log(f"promotion PR already open: {urls[0]['url']} (updated automatically by the push)")
        return urls[0]["url"]

    changelog = "\n".join(f"- {line}" for line in commits.splitlines())
    # "Fixes #N" in PRs merged to the working branch never reaches GitHub's
    # auto-close (it only fires on the default branch), so the promotion PR
    # must carry the Closes lines itself.
    open_issues_json = run(
        ["gh", "issue", "list", "--state", "open", "--limit", "1000", "--json", "number"],
        cwd=project_root,
    )
    open_issues = {item["number"] for item in json.loads(open_issues_json.stdout)}
    closes = closable_issue_refs(commits.splitlines(), open_issues)
    closes_section = (
        "\n\n## Closes on merge\n\n"
        + "\n".join(f"Closes #{n}" for n in closes)
        + "\n\nPrune any line whose issue shouldn't auto-close (partial fixes, "
        "device verification pending)."
        if closes
        else ""
    )
    body = (
        f"Promotion of `{working}` into `{stable}` — human verification required.\n\n"
        f"## Changes\n\n{changelog}{closes_section}\n\n"
        "Verify on staging, then merge **with a merge commit** (`M` in gh-dash) — "
        "never squash. A squashed promotion puts a commit on the stable branch "
        "that the working branch's history doesn't contain; the branches diverge "
        "and every later promotion hits phantom conflicts. "
        "Do NOT let an agent merge this."
    )
    proc = run(
        [
            "gh",
            "pr",
            "create",
            "--base",
            stable,
            "--head",
            working,
            "--title",
            f"release: promote {working} to {stable}",
            "--body",
            body,
        ],
        cwd=project_root,
    )
    url = proc.stdout.strip().splitlines()[-1]
    log(f"promotion PR opened: {url}")
    return url
