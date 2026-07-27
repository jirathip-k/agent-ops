from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import yaml

from agent_ops.registry import EXAMPLE_REGISTRY_FILE, REGISTRY_FILE, RegistryConfig
from agent_ops.utils import CommandError, run

BUCKETS = ("agent-ready", "needs-human", "backlog")

# evolve and distill are lanes (agent evolve/distill validate against this
# tuple) but neither has a CI caller here: evolve's cadence is #153, and
# distill is dispatch-only by deliberate decision (#198) — it never gets a
# stub with a `schedule:`. Both are still listed so `agent status` and lane
# validation treat them as real lanes rather than accidentally-valid names
# that merely happen to have a prompt file.
LANES = ("triage", "groom", "promote", "spec", "plan", "scout", "evolve", "distill")

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


@dataclass(frozen=True)
class LaneInfo:
    """How a repo wires one lane: runner label passed (None = pipeline default)
    and the cron expression of the calling stub's `on: schedule:` trigger
    (None = not cron-scheduled)."""

    runner: str | None
    cron: str | None


def detect_lanes(workflows: dict[str, str]) -> dict[str, LaneInfo]:
    """Map each lane a repo has wired up to how its calling stub wires it.

    Content-based on purpose: stub filenames vary per repo (triage.yml,
    groom.yml, ...), but every caller must `uses:` a reusable
    `<lane>-pipeline.yml`, so that reference is the source of truth. The
    runner is the `runner:` input in the calling job's `with:` block, or
    None when the stub passes none (pipeline default ubuntu-latest). The
    cron comes from the stub file's `on: schedule:` trigger and applies to
    every lane that file calls; workflow_dispatch-only stubs get cron=None.
    If two jobs/files call the same lane, the last one wins.
    """
    lanes: dict[str, LaneInfo] = {}
    for content in workflows.values():
        lanes.update(_lanes_in(content))
    return lanes


def _lanes_in(content: str) -> Iterator[tuple[str, LaneInfo]]:
    try:
        doc = yaml.safe_load(content)
    except yaml.YAMLError:
        # Unparseable file: fall back to a plain line scan (runner/cron unknown).
        for match in _USES_LINE_RE.finditer(content):
            yield match.group(1), LaneInfo(runner=None, cron=None)
        return
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return
    cron = _file_cron(doc)
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        match = _USES_VALUE_RE.fullmatch(uses) if isinstance(uses, str) else None
        if match is None:
            continue
        with_block = job.get("with")
        runner = with_block.get("runner") if isinstance(with_block, dict) else None
        yield match.group(1), LaneInfo(str(runner) if runner is not None else None, cron)


def _file_cron(doc: dict[Any, Any]) -> str | None:
    """First cron expression under the file's `on: schedule:` trigger, if any.

    YAML 1.1 parses a bare `on` key as boolean True, so look under both.
    """
    triggers = doc.get("on", doc.get(True))
    schedule = triggers.get("schedule") if isinstance(triggers, dict) else None
    if not isinstance(schedule, list):
        return None
    for entry in schedule:
        if isinstance(entry, dict) and entry.get("cron") is not None:
            return str(entry["cron"])
    return None


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


def _cell(info: LaneInfo) -> str:
    """Matrix cell: shorthand runner, starred when the lane is cron-scheduled."""
    return _short_runner(info.runner) + ("*" if info.cron is not None else "")


def pipeline_coverage(config: RegistryConfig, log: Callable[[str], None] = print) -> None:
    """Matrix of which reusable agent-ops CI lanes each registered repo has wired up.

    Derived live from each repo's .github/workflows via the GitHub API —
    no stored state (ADR 0003: state lives in GitHub). Cells show the runner
    each lane's stub passes; `gh` means the pipeline default (ubuntu-latest);
    a trailing `*` marks lanes whose calling stub has a cron schedule.
    """
    rows = [(repo, detect_lanes(_repo_workflows(repo))) for repo in config.repos]
    cells = {
        repo: {lane: _cell(lanes[lane]) if lane in lanes else "–" for lane in LANES}
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
    log("\n* = cron-scheduled; gh = pipeline default ubuntu-latest")


# Failed runs shown per repo. Enough to see whether a repo is failing
# repeatedly or once, short enough that a bad fleet still fits one screen.
FAILURE_LIMIT = 5

# Per-repo bound for the failure sweep. Each `gh run list` is a single API
# round-trip, so a repo still silent after this long is wedged (a credential
# prompt, a hung proxy), not slow — and the sweep is a fan-out, where the
# fleet-wide default would let one such repo hold every remaining repo behind
# it for two full minutes. Tight enough to keep the sweep interactive,
# generous enough for a bad network day. The per-conclusion asks below share
# this bound rather than dividing it: `run()` raises on the first expiry, so a
# wedged repo still costs one timeout and then gets skipped, not four.
FAILURE_TIMEOUT_S = 30.0

# Conclusions that mean a run did not do its job. `failure` is only the
# ordinary one: a caller that has drifted from the reusable pipeline it calls
# never starts at all (`startup_failure`), a wedged run gets `cancelled` by a
# human or by a re-run, and a hung one ends `timed_out`. Sweeping for
# `failure` alone reported repos whose triage lane had been dead for days —
# five straight `startup_failure` runs — as clean.
FAILED_CONCLUSIONS = ("failure", "startup_failure", "cancelled", "timed_out")

# Column tag per conclusion: short enough to sit between the workflow name and
# the timestamp without pushing URLs off the right edge, distinct enough that
# `startup` never reads as an ordinary red build.
CONCLUSION_TAGS = {
    "failure": "failed",
    "startup_failure": "startup",
    "cancelled": "cancelled",
    "timed_out": "timed-out",
}


def _tag(run_info: dict[str, Any]) -> str:
    """Short label for why a run counted as failed; unknown values pass through
    verbatim rather than being flattened into a misleading `failed`."""
    conclusion = str(run_info.get("conclusion") or "?")
    return CONCLUSION_TAGS.get(conclusion, conclusion)


def _runs_with_conclusion(repo: str, conclusion: str, limit: int) -> list[dict[str, Any]]:
    """The `limit` most recent runs of `repo` with exactly this conclusion.

    Raises `CommandError` for anything that makes the repo unreadable — 404,
    no access, renamed, wedged `gh`. The sweep turns that into a skip: one
    unreachable repo must not take the rest of the fleet down with it.
    """
    proc = run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--status",
            conclusion,
            "--limit",
            str(limit),
            "--json",
            "name,conclusion,headBranch,createdAt,url",
        ],
        check=False,
        timeout=FAILURE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        # `gh` puts the useful part last ("could not resolve to a Repository",
        # "HTTP 404"); the lines above it are boilerplate nobody scanning a
        # fleet sweep needs.
        detail = proc.stderr.strip().splitlines()
        raise CommandError(detail[-1] if detail else f"gh run list exited {proc.returncode}")
    return json.loads(proc.stdout)


def _failed_runs(repo: str, limit: int) -> list[dict[str, Any]]:
    """The `limit` most recent failed workflow runs for `repo`, newest first.

    `gh run list --status` takes one value, so this asks once per conclusion in
    `FAILED_CONCLUSIONS` and merges by recency. Asking beats widening the
    window and filtering locally: a repo with busy CI could bury a dead lane
    under any fixed number of green runs, which is the blind spot this sweep
    exists to close.
    """
    merged: list[dict[str, Any]] = []
    for conclusion in FAILED_CONCLUSIONS:
        merged.extend(_runs_with_conclusion(repo, conclusion, limit))
    merged.sort(key=lambda info: str(info.get("createdAt", "")), reverse=True)
    return merged[:limit]


def _when(created_at: str) -> str:
    """`2026-07-26T08:14:22Z` → `07-26 08:14`; anything else passes through."""
    match = re.match(r"\d{4}-(\d{2}-\d{2})T(\d{2}:\d{2})", created_at)
    return f"{match.group(1)} {match.group(2)}" if match else created_at


# Longest branch name shown before eliding. Long enough for the lane branches
# this platform creates (`fix/issue-123`), short enough that one outlier
# branch can't push a whole repo's URLs off the right edge.
BRANCH_WIDTH = 24


def _branch(run_info: dict[str, Any]) -> str:
    """Head branch, elided in the middle — the prefix says which lane made it,
    the suffix says which issue, and the middle is the part nobody scans."""
    branch = str(run_info.get("headBranch", "?"))
    if len(branch) <= BRANCH_WIDTH:
        return branch
    keep = BRANCH_WIDTH - 1
    return f"{branch[: keep // 2]}…{branch[-(keep - keep // 2) :]}"


def fleet_failures(
    config: RegistryConfig,
    log: Callable[[str], None] = print,
    limit: int = FAILURE_LIMIT,
) -> None:
    """Recent failed workflow runs across every registered repo.

    Runs under the operator's local `gh` auth, like `fleet_status` and
    `pipeline_coverage` — there is no cross-repo credential in CI (issue #95).
    Only repos with failures get a section, so a clean fleet is one line;
    repos that can't be read are named and skipped, never fatal.
    """
    if not config.repos:
        log(f"no repos registered — copy {EXAMPLE_REGISTRY_FILE.name} to {REGISTRY_FILE}")
        return

    failing = clean = 0
    skipped: list[str] = []
    startup_repos: list[str] = []
    for repo in config.repos:
        try:
            runs = _failed_runs(repo, limit)
        except CommandError as exc:
            skipped.append(repo)
            log(f"\033[1m{repo}\033[0m — skipped: {exc}")
            continue
        if not runs:
            clean += 1
            continue
        failing += 1
        # A run that never started is a contract problem, not a flaky build, so
        # its count belongs in the header where the reader decides what to open.
        startup = sum(1 for r in runs if r.get("conclusion") == "startup_failure")
        if startup:
            startup_repos.append(repo)
        note = f" · ⚠ {startup} startup_failure" if startup else ""
        log(f"\n\033[1m{repo}\033[0m — {len(runs)} recent failed run(s){note}")
        # Columns are sized per repo, not per fleet: one repo's long workflow
        # names would otherwise indent every other repo's URLs off the screen.
        rows = [
            (
                str(r.get("name", "?")),
                _tag(r),
                _when(str(r.get("createdAt", ""))),
                _branch(r),
                r.get("url"),
            )
            for r in runs
        ]
        name_w = max(len(name) for name, _, _, _, _ in rows)
        tag_w = max(len(tag) for _, tag, _, _, _ in rows)
        branch_w = max(len(branch) for _, _, _, branch, _ in rows)
        for name, tag, when, branch, url in rows:
            log(
                f"  {name.ljust(name_w)}  {tag.ljust(tag_w)}  {when}  "
                f"{branch.ljust(branch_w)}  {url or ''}"
            )

    if failing:
        parts = [f"{failing} repo(s) with failures", f"{clean} clean"]
    else:
        parts = [f"✓ {clean} repo(s) clean — no recent failed runs"]
    if skipped:
        parts.append(f"{len(skipped)} skipped ({', '.join(skipped)})")
    log(("\n" if failing else "") + " · ".join(parts))
    if startup_repos:
        log(
            "\n⚠ startup_failure = the run never started, almost always caller/callee "
            f"contract drift. Run `agent doctor` in: {', '.join(startup_repos)}"
        )


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
