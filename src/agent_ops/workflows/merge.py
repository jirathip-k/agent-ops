from __future__ import annotations

import json
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from agent_ops.config import ProjectConfig, load_project_config
from agent_ops.utils import CommandError, run


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
    if changed_lines > config.merge.max_changed_lines:
        violations.append(f"{changed_lines} changed lines > cap {config.merge.max_changed_lines}")
    if len(files) > config.merge.max_changed_files:
        violations.append(f"{len(files)} changed files > cap {config.merge.max_changed_files}")

    for f in files:
        for pattern in config.merge.blocked_paths:
            # case-insensitive: useAuth.ts must match *auth*
            if fnmatch(f["path"].lower(), pattern.lower()):
                violations.append(f"blocked path: {f['path']} (matches {pattern!r})")
                break
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
    pr = json.loads(proc.stdout)
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

    run(["git", "fetch", "origin", working, stable], cwd=project_root)
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
    body = (
        f"Promotion of `{working}` into `{stable}` — human verification required.\n\n"
        f"## Changes\n\n{changelog}\n\n"
        "Verify on staging, then merge (do NOT let an agent merge this)."
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
