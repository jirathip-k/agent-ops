from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent_ops import github, worktree
from agent_ops.config import load_project_config
from agent_ops.prompts import render_task
from agent_ops.utils import run
from agent_ops.workflows.implement import _labels, role_request


def run_spec(
    project_root: Path,
    issue_number: int,
    *,
    post: bool = True,
    runtime_override: str | None = None,
    log: Callable[[str], None] = print,
) -> str:
    """Elaborate a parked idea into an agent-ready spec (read-only, smart model).

    Explores the code, enumerates every affected surface, and writes
    checklist acceptance criteria. Posts the spec as an issue comment so the
    human only has to flip the label to agent-ready. Raises on ESCALATE —
    the spec agent found a question only a human can answer.
    """
    config = load_project_config(project_root)
    issue = github.get_issue(issue_number, cwd=project_root)
    prompt = render_task(
        "spec",
        issue_number=str(issue["number"]),
        issue_title=issue["title"],
        issue_body=issue.get("body") or "(no description)",
        issue_labels=_labels(issue),
    )
    # Spec against the WORKING branch — same reasoning as triage/groom:
    # merged-but-unpromoted surfaces live there, not on the local checkout.
    run(["git", "fetch", "origin", config.base_branch], cwd=project_root)
    task_id = f"spec-{issue_number}-tmp"
    spec_wt = worktree.create_detached(
        project_root, config.worktree_dir, task_id, f"origin/{config.base_branch}"
    )
    try:
        runtime, request = role_request(
            config,
            "planner",
            prompt,
            spec_wt,
            runtime_override=runtime_override,
            # read-only gh: issue comments carry follow-ups; merged PRs show
            # what already landed
            extra_allowed_tools=(
                "Bash(gh issue view:*)",
                "Bash(gh issue list:*)",
                "Bash(gh search issues:*)",
                "Bash(gh pr list:*)",
                "Bash(gh pr view:*)",
            ),
        )
        result = runtime.run(request)
    finally:
        worktree.remove(project_root, config.worktree_dir, task_id, force=True)
    if not result.ok:
        raise RuntimeError(f"Spec run failed: {result.text}")
    if result.text.lstrip().upper().startswith("ESCALATE"):
        raise RuntimeError(f"Spec agent escalated:\n{result.text}")

    if post:
        github.comment_on_issue(issue_number, f"## Agent spec\n\n{result.text}", cwd=project_root)
        log(f"posted spec on issue #{issue_number}")
    return result.text
