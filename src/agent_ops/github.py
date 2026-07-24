from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agent_ops.utils import CommandError, run


def get_issue(number: int, cwd: Path) -> dict[str, Any]:
    proc = run(
        ["gh", "issue", "view", str(number), "--json", "number,title,body,labels,url"],
        cwd=cwd,
    )
    return json.loads(proc.stdout)


def create_pr(cwd: Path, *, base: str, title: str, body: str) -> str:
    proc = run(
        ["gh", "pr", "create", "--base", base, "--title", title, "--body", body],
        cwd=cwd,
    )
    return proc.stdout.strip()


def pr_view(number: int, cwd: Path) -> dict[str, Any]:
    proc = run(
        ["gh", "pr", "view", str(number), "--json", "number,title,body,url,baseRefName"],
        cwd=cwd,
    )
    return json.loads(proc.stdout)


def pr_diff(number: int, cwd: Path) -> str:
    return run(["gh", "pr", "diff", str(number)], cwd=cwd).stdout


def comment_on_pr(number: int, body: str, cwd: Path) -> None:
    run(["gh", "pr", "comment", str(number), "--body", body], cwd=cwd)


def comment_on_issue(number: int, body: str, cwd: Path) -> None:
    run(["gh", "issue", "comment", str(number), "--body", body], cwd=cwd)


def pr_references_issue(pr: dict[str, Any], issue_number: int) -> bool:
    """True if this PR's branch or title/body plausibly fixes the given issue."""
    if pr.get("headRefName") == f"fix/issue-{issue_number}":
        return True
    pattern = re.compile(rf"(?<!\d)#{issue_number}(?!\d)")
    text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
    return bool(pattern.search(text))


def open_prs_for_issue(issue_number: int, cwd: Path) -> list[dict[str, Any]]:
    """Open PRs that already reference this issue, for the dedupe guard.

    Fails open (returns []) when `gh` can't answer — e.g. no remote in a
    test/scratch repo — since without a GitHub remote no duplicate PR can
    exist, and the run would fail later at push/PR anyway.
    """
    try:
        proc = run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--json",
                "number,title,body,headRefName,url",
                "--limit",
                "100",
            ],
            cwd=cwd,
        )
    except CommandError:
        return []
    prs: list[dict[str, Any]] = json.loads(proc.stdout)
    return [pr for pr in prs if pr_references_issue(pr, issue_number)]
