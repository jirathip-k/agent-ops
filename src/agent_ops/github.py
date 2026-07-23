from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_ops.utils import run


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
