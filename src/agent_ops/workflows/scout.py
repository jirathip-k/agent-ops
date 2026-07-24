from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_ops import worktree
from agent_ops.config import load_project_config
from agent_ops.prompts import render_task
from agent_ops.utils import run
from agent_ops.workflows.implement import role_request
from agent_ops.workflows.triage import LABEL_COLORS

_RESULT_LINE = re.compile(r"^#(\d+)\s*[—-]+\s*(.+)$")


@dataclass(frozen=True)
class ScoutResult:
    number: int
    reason: str


def parse_scout(text: str) -> list[ScoutResult] | None:
    """Parse the SCOUT RESULTS block. None = no block; [] = explicit 'none'."""
    _, marker, tail = text.rpartition("SCOUT RESULTS:")
    if not marker:
        return None
    results = []
    for line in tail.strip().splitlines():
        line = line.strip()
        if line.lower() == "none":
            return []
        m = _RESULT_LINE.match(line)
        if m:
            results.append(ScoutResult(int(m.group(1)), m.group(2).strip()))
    return results


def run_scout(
    project_root: Path,
    *,
    max_issues: int = 3,
    log: Callable[[str], None] = print,
) -> list[ScoutResult]:
    """Mine existing signals (TODOs, deferred review threads, swallowed errors,
    untested modules) and file at most `max_issues` backlog issues.

    Filed issues carry `backlog` + `proposed-by-agent` and enter the normal
    groom/spec funnel — scouting generates candidates, never dispatches work.
    """
    config = load_project_config(project_root)

    # The agent files issues with these labels mid-run — they must exist first.
    for name in ("backlog", "proposed-by-agent"):
        run(
            ["gh", "label", "create", name, "--color", LABEL_COLORS[name], "--force"],
            cwd=project_root,
            check=False,
        )

    # Scout against the WORKING branch — a TODO already resolved on staging
    # must not become an issue.
    run(["git", "fetch", "origin", config.base_branch], cwd=project_root)
    scout_wt = worktree.create_detached(
        project_root, config.worktree_dir, "scout-tmp", f"origin/{config.base_branch}"
    )
    try:
        runtime, request = role_request(
            config,
            "planner",
            render_task("scout", max_issues=str(max_issues)),
            scout_wt,
            # may FILE issues (never fix); reads merged PRs for deferral threads
            extra_allowed_tools=(
                "Bash(gh issue create:*)",
                "Bash(gh issue list:*)",
                "Bash(gh issue view:*)",
                "Bash(gh search issues:*)",
                "Bash(gh pr list:*)",
                "Bash(gh pr view:*)",
            ),
        )
        result = runtime.run(request)
    finally:
        worktree.remove(project_root, config.worktree_dir, "scout-tmp", force=True)
    if not result.ok:
        raise RuntimeError(f"Scout run failed: {result.text}")

    results = parse_scout(result.text)
    if results is None:
        raise RuntimeError(f"Scout produced no parseable results:\n{result.text[-500:]}")
    if not results:
        log("scout filed nothing — no candidate cleared the bar")
        return []
    for r in results[:max_issues]:
        log(f"#{r.number} filed: {r.reason}")
    if len(results) > max_issues:
        log(f"warning: scout reported {len(results)} issues, cap was {max_issues}")
    return results
