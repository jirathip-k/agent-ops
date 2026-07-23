from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_ops import worktree
from agent_ops.config import load_project_config
from agent_ops.prompts import render_task
from agent_ops.utils import run
from agent_ops.workflows.implement import role_request

# An issue is classified only when it carries a bucket label. The CI lane
# stamps `triage:done` on everything it processes but (report-only) leaves
# bugs bucketless — those must still be triaged here, or the queue starves.
BUCKET_LABELS = {"agent-ready", "needs-human", "backlog"}

LABEL_COLORS = {
    "agent-ready": "1d76db",
    "needs-human": "d93f0b",
    "backlog": "c5def5",
    "found-by-audit": "fbca04",
}

_RESULT_LINE = re.compile(r"^#(\d+)\s+(agent-ready|needs-human|backlog)\s*[—-]+\s*(.+)$")


@dataclass(frozen=True)
class TriageResult:
    number: int
    verdict: str
    reason: str


def parse_triage(text: str) -> list[TriageResult]:
    """Parse the TRIAGE RESULTS block from the agent's final message."""
    _, marker, tail = text.rpartition("TRIAGE RESULTS:")
    if not marker:
        return []
    results = []
    for line in tail.strip().splitlines():
        m = _RESULT_LINE.match(line.strip())
        if m:
            results.append(TriageResult(int(m.group(1)), m.group(2), m.group(3).strip()))
    return results


def run_triage(
    project_root: Path,
    *,
    dispatch: bool = False,
    log: Callable[[str], None] = print,
) -> list[TriageResult]:
    """Classify untriaged open issues; label + comment each; optionally dispatch.

    agent-ready issues get dispatched (with `dispatch=True`) onto the most
    visible surface, running the full implement pipeline — which, with
    `loop.auto_merge`, carries a passing change all the way into staging.
    """
    config = load_project_config(project_root)
    proc = run(
        [
            "gh",
            "issue",
            "list",
            "--state",
            "open",
            "--limit",
            "50",
            "--json",
            "number,title,body,labels",
        ],
        cwd=project_root,
    )
    issues = [
        i
        for i in json.loads(proc.stdout)
        if not BUCKET_LABELS & {lbl["name"] for lbl in i.get("labels", [])}
    ]
    if not issues:
        log("nothing to triage — every open issue is already classified")
        return []
    log(f"triaging {len(issues)} issue(s)")

    issues_text = "\n\n".join(
        f"### #{i['number']}: {i['title']}\n{i.get('body') or '(no description)'}" for i in issues
    )

    # Classify against the WORKING branch (staging), not the local checkout —
    # the checkout may sit on a stale main while merged work lives on staging.
    run(["git", "fetch", "origin", config.base_branch], cwd=project_root)
    triage_wt = worktree.create_detached(
        project_root, config.worktree_dir, "triage-tmp", f"origin/{config.base_branch}"
    )
    try:
        runtime, request = role_request(
            config,
            "planner",
            render_task("triage", issues=issues_text),
            triage_wt,
            # triage may FILE audit issues it discovers (never fix them);
            # search first to avoid duplicates
            extra_allowed_tools=(
                "Bash(gh issue create:*)",
                "Bash(gh issue list:*)",
                "Bash(gh search issues:*)",
            ),
        )
        result = runtime.run(request)
    finally:
        worktree.remove(project_root, config.worktree_dir, "triage-tmp", force=True)
    if not result.ok:
        raise RuntimeError(f"Triage run failed: {result.text}")

    results = parse_triage(result.text)
    if not results:
        raise RuntimeError(f"Triage produced no parseable results:\n{result.text[-500:]}")

    for name, color in LABEL_COLORS.items():
        run(
            ["gh", "label", "create", name, "--color", color, "--force"],
            cwd=project_root,
            check=False,
        )

    for r in results:
        run(["gh", "issue", "edit", str(r.number), "--add-label", r.verdict], cwd=project_root)
        run(
            [
                "gh",
                "issue",
                "comment",
                str(r.number),
                "--body",
                f"**Triage: {r.verdict}** — {r.reason}",
            ],
            cwd=project_root,
        )
        log(f"#{r.number} → {r.verdict}: {r.reason}")

    if dispatch:
        from agent_ops import surfaces  # local import: surfaces pulls in subprocess spawning

        for r in results:
            if r.verdict == "agent-ready":
                where = surfaces.pick("auto").spawn(
                    f"agent-issue-{r.number}",
                    ["agent", "implement", str(r.number), "--project", str(project_root)],
                    project_root,
                )
                log(f"#{r.number} dispatched → {where}")
    return results
