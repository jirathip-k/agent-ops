from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from agent_ops.board import BoardConfig
from agent_ops.utils import run

BUCKETS = ("agent-ready", "needs-human", "backlog")


def bucket_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
    """Count open issues per triage bucket; anything unlabeled is 'untriaged'."""
    counts: dict[str, int] = {bucket: 0 for bucket in (*BUCKETS, "untriaged")}
    for issue in issues:
        labels = {lbl["name"] for lbl in issue.get("labels", [])}
        for bucket in BUCKETS:
            if bucket in labels:
                counts[bucket] += 1
                break
        else:
            counts["untriaged"] += 1
    return counts


def fleet_status(config: BoardConfig, log: Callable[[str], None] = print) -> None:
    """One screen: every registered repo's open PRs and issue buckets."""
    for repo in config.repos:
        prs = json.loads(
            run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "open",
                    "--limit",
                    "20",
                    "--json",
                    "number,title,baseRefName,headRefName",
                ],
            ).stdout
        )
        issues = json.loads(
            run(
                [
                    "gh",
                    "issue",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "open",
                    "--limit",
                    "200",
                    "--json",
                    "labels",
                ],
            ).stdout
        )
        counts = bucket_counts(issues)
        log(
            f"\n\033[1m{repo}\033[0m — {len(issues)} open issue(s): "
            + " · ".join(f"{v} {k}" for k, v in counts.items() if v)
        )
        for pr in prs:
            promo = " ⚠ PROMOTION (yours)" if pr["headRefName"] == "staging" else ""
            title = pr["title"] if len(pr["title"]) <= 70 else pr["title"][:69] + "…"
            log(f"  PR #{pr['number']} → {pr['baseRefName']}{promo}  {title}")
        if not prs:
            log("  no open PRs")
