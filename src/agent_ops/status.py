from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable
from typing import Any

from agent_ops.registry import RegistryConfig
from agent_ops.utils import CommandError, run

BUCKETS = ("agent-ready", "needs-human", "backlog")

LANES = ("triage", "groom", "promote", "spec", "plan")

# Name of the control repo hosting the reusable pipelines. Detection accepts
# any owner prefix (`<owner>/agent-ops/...`) so forks keep working, plus the
# local form (`./.github/workflows/...`) the control repo itself could use.
CONTROL_REPO = "agent-ops"

_USES_RE = re.compile(
    rf"^\s*uses:\s*(?:[\w.-]+/{CONTROL_REPO}|\.)/\.github/workflows/"
    rf"({'|'.join(LANES)})-pipeline\.ya?ml(?:@\S+)?\s*$",
    re.MULTILINE,
)


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


def detect_lanes(workflows: dict[str, str]) -> set[str]:
    """Lanes a repo has wired up, judged by `uses:` references in workflow content.

    Content-based on purpose: stub filenames vary per repo (triage.yml,
    groom.yml, ...), but every caller must `uses:` a reusable
    `<lane>-pipeline.yml`, so that reference is the source of truth.
    """
    lanes: set[str] = set()
    for content in workflows.values():
        lanes.update(match.group(1) for match in _USES_RE.finditer(content))
    return lanes


def _repo_workflows(repo: str) -> dict[str, str]:
    """Fetch {filename: content} for a repo's .github/workflows via the GitHub API."""
    listing = run(["gh", "api", f"repos/{repo}/contents/.github/workflows"], check=False)
    if listing.returncode != 0:
        if "404" in listing.stderr:
            return {}  # no workflows dir at all — simply no lanes wired
        raise CommandError(f"gh api failed for {repo}: {listing.stderr.strip()}")
    workflows: dict[str, str] = {}
    for entry in json.loads(listing.stdout):
        name: str = entry["name"]
        if not name.endswith((".yml", ".yaml")):
            continue
        blob = run(
            ["gh", "api", f"repos/{repo}/contents/.github/workflows/{name}", "--jq", ".content"]
        )
        workflows[name] = base64.b64decode(blob.stdout).decode()
    return workflows


def pipeline_coverage(config: RegistryConfig, log: Callable[[str], None] = print) -> None:
    """Matrix of which reusable agent-ops CI lanes each registered repo has wired up.

    Derived live from each repo's .github/workflows via the GitHub API —
    no stored state (ADR 0003: state lives in GitHub).
    """
    name_width = max((len(repo) for repo in config.repos), default=0)
    log("")
    log(" " * name_width + "  " + "  ".join(LANES))
    for repo in config.repos:
        lanes = detect_lanes(_repo_workflows(repo))
        cells = "  ".join(("✓" if lane in lanes else "–").center(len(lane)) for lane in LANES)
        note = "" if lanes else "  ⚠ no agent-ops lanes wired"
        log(f"\033[1m{repo.ljust(name_width)}\033[0m  {cells}{note}")


def fleet_status(config: RegistryConfig, log: Callable[[str], None] = print) -> None:
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
