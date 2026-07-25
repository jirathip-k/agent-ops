from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_ops.utils import CommandError, run


def get_issue(number: int, cwd: Path) -> dict[str, Any]:
    proc = run(
        ["gh", "issue", "view", str(number), "--json", "number,title,body,labels,url,comments"],
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
    """True if this PR's branch matches or it will actually close the given issue.

    A bare `#N` mention in the title/body (e.g. cross-referencing a related but
    out-of-scope issue) does NOT count — only a real GitHub closing reference
    (`Fixes`/`Closes`/`Resolves` `#N`, as GitHub itself parses it, surfaced via
    `closingIssuesReferences`) or the conventional `fix/issue-N` branch name.
    """
    if pr.get("headRefName") == f"fix/issue-{issue_number}":
        return True
    closing_refs = pr.get("closingIssuesReferences") or []
    return any(ref.get("number") == issue_number for ref in closing_refs)


def open_pr_numbers(base: str, cwd: Path) -> list[int]:
    """Numbers of every open PR targeting `base`, ascending."""
    proc = run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--base",
            base,
            "--json",
            "number",
            "--limit",
            "100",
        ],
        cwd=cwd,
    )
    prs: list[dict[str, Any]] = json.loads(proc.stdout)
    return sorted(pr["number"] for pr in prs)


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
                "number,title,body,headRefName,url,closingIssuesReferences",
                "--limit",
                "100",
            ],
            cwd=cwd,
        )
    except CommandError:
        return []
    prs: list[dict[str, Any]] = json.loads(proc.stdout)
    return [pr for pr in prs if pr_references_issue(pr, issue_number)]
