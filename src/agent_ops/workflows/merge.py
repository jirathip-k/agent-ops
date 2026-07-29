from __future__ import annotations

import json
import re
from collections.abc import Callable
from fnmatch import fnmatch, fnmatchcase
from pathlib import Path
from typing import Any

from agent_ops.config import ProjectConfig, load_project_config
from agent_ops.runs import Run, discover_runs, issue_from_branch
from agent_ops.utils import SLOW_GIT_TIMEOUT_S, CommandError, run


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
            "baseRefName,headRefName,title,url,files,state,labels",
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
    run(["gh", "pr", "merge", str(pr_number), "--squash"], cwd=project_root)
    run(
        ["git", "push", "origin", "--delete", pr["headRefName"]],
        cwd=project_root,
        check=False,
    )
    log(f"merged PR #{pr_number} ({pr['title']}) into {pr['baseRefName']}")
    return True


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
