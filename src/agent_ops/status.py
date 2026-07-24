from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Iterator
from typing import Any

import yaml

from agent_ops.registry import RegistryConfig
from agent_ops.utils import CommandError, run

BUCKETS = ("agent-ready", "needs-human", "backlog")

LANES = ("triage", "groom", "promote", "spec", "plan")

# Name of the control repo hosting the reusable pipelines. Detection accepts
# any owner prefix (`<owner>/agent-ops/...`) so forks keep working, plus the
# local form (`./.github/workflows/...`) the control repo itself could use.
CONTROL_REPO = "agent-ops"

_USES_PATH = (
    rf"(?:[\w.-]+/{CONTROL_REPO}|\.)/\.github/workflows/"
    rf"({'|'.join(LANES)})-pipeline\.ya?ml(?:@\S+)?"
)
_USES_VALUE_RE = re.compile(_USES_PATH)
_USES_LINE_RE = re.compile(rf"^\s*uses:\s*{_USES_PATH}\s*$", re.MULTILINE)


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


def detect_lanes(workflows: dict[str, str]) -> dict[str, str | None]:
    """Map each lane a repo has wired up to the runner label its stub passes.

    Content-based on purpose: stub filenames vary per repo (triage.yml,
    groom.yml, ...), but every caller must `uses:` a reusable
    `<lane>-pipeline.yml`, so that reference is the source of truth. The
    value is the `runner:` input in the calling job's `with:` block, or
    None when the stub passes none (pipeline default ubuntu-latest). If two
    jobs/files call the same lane, the last one wins.
    """
    lanes: dict[str, str | None] = {}
    for content in workflows.values():
        lanes.update(_lanes_in(content))
    return lanes


def _lanes_in(content: str) -> Iterator[tuple[str, str | None]]:
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError:
        # Unparseable file: fall back to a plain line scan (runner unknown).
        for match in _USES_LINE_RE.finditer(content):
            yield match.group(1), None
        return
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        match = _USES_VALUE_RE.fullmatch(uses) if isinstance(uses, str) else None
        if match is None:
            continue
        with_block = job.get("with")
        runner = with_block.get("runner") if isinstance(with_block, dict) else None
        yield match.group(1), str(runner) if runner is not None else None


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


def _short_runner(runner: str | None) -> str:
    """Compress runner labels so the matrix stays readable.

    blacksmith-2vcpu-ubuntu-2404 → bs-2vcpu; macOS Blacksmith labels get a
    -mac suffix; no runner passed → gh (pipeline default ubuntu-latest).
    """
    if runner is None:
        return "gh"
    match = re.fullmatch(r"blacksmith-(\d+vcpu)-([\w.-]+)", runner)
    if match:
        suffix = "-mac" if "mac" in match.group(2) else ""
        return f"bs-{match.group(1)}{suffix}"
    return runner


def pipeline_coverage(config: RegistryConfig, log: Callable[[str], None] = print) -> None:
    """Matrix of which reusable agent-ops CI lanes each registered repo has wired up.

    Derived live from each repo's .github/workflows via the GitHub API —
    no stored state (ADR 0003: state lives in GitHub). Cells show the runner
    each lane's stub passes; `gh` means the pipeline default (ubuntu-latest).
    """
    rows = [(repo, detect_lanes(_repo_workflows(repo))) for repo in config.repos]
    cells = {
        repo: {lane: _short_runner(lanes[lane]) if lane in lanes else "–" for lane in LANES}
        for repo, lanes in rows
    }
    name_width = max((len(repo) for repo, _ in rows), default=0)
    widths = {
        lane: max([len(lane), *(len(cells[repo][lane]) for repo, _ in rows)]) for lane in LANES
    }
    log("")
    log(" " * name_width + "  " + "  ".join(lane.center(widths[lane]) for lane in LANES))
    for repo, lanes in rows:
        row = "  ".join(cells[repo][lane].center(widths[lane]) for lane in LANES)
        note = "" if lanes else "  ⚠ no agent-ops lanes wired"
        log(f"\033[1m{repo.ljust(name_width)}\033[0m  {row}{note}")
    log("\ngh = pipeline default ubuntu-latest")


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
