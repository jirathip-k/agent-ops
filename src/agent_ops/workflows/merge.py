from __future__ import annotations

import json
import re
from collections.abc import Callable
from fnmatch import fnmatch, fnmatchcase
from pathlib import Path
from typing import Any

from agent_ops.config import ProjectConfig, load_project_config
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
    """Return the list of rule violations blocking an agent merge (empty = mergeable)."""
    violations: list[str] = []

    if pr["baseRefName"] != config.base_branch:
        violations.append(
            f"base is {pr['baseRefName']!r}, agents may only merge into "
            f"{config.base_branch!r} (stable branch {config.merge.stable_branch!r} "
            f"is human-only)"
        )
    if pr["baseRefName"] == config.merge.stable_branch:
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
    return violations


def _fetch_pr(project_root: Path, pr_number: int) -> dict[str, Any]:
    proc = run(
        [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "baseRefName,headRefName,title,url,files,state",
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
    log: Callable[[str], None] = print,
) -> bool:
    """Squash-merge a PR into the working branch if every rule passes.

    Rules: base must be the working branch, CI green (missing checks warn),
    diff within caps, no blocked paths. `override=True` merges anyway but
    logs every overridden rule — that is a human decision, never automate it.
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
