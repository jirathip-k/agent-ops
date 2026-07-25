from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent_ops import github, surfaces
from agent_ops.config import load_project_config
from agent_ops.prompts import render_task
from agent_ops.workflows.implement import role_request


def run_review(
    project_root: Path,
    pr_number: int,
    *,
    runtime_name: str | None = None,
    post_comment: bool = False,
    log: Callable[[str], None] = print,
) -> str:
    """Run the reviewer role (read-only) over a PR diff; optionally post the result."""
    config = load_project_config(project_root)
    pr = github.pr_view(pr_number, cwd=project_root)
    diff = github.pr_diff(pr_number, cwd=project_root)
    prompt = render_task(
        "review",
        diff=diff,
        context=f"PR #{pr['number']}: {pr['title']}\n\n{pr.get('body') or ''}",
    )

    runtime, request = role_request(
        config, "reviewer", prompt, project_root, runtime_override=runtime_name
    )
    result = runtime.run(request)
    if not result.ok:
        raise RuntimeError(f"Review run failed: {result.text}")

    if post_comment:
        github.comment_on_pr(pr_number, f"## Agent review\n\n{result.text}", cwd=project_root)
        log(f"posted review comment on PR #{pr_number}")
    return result.text


def review_command(
    project_root: Path,
    pr_number: int,
    *,
    post_comment: bool = False,
    runtime_name: str | None = None,
) -> list[str]:
    """Argv that re-runs this review inline, for spawning onto a surface."""
    command = ["agent", "review", str(pr_number)]
    if post_comment:
        command.append("--post")
    if runtime_name:
        command += ["--runtime", runtime_name]
    return command + ["--project", str(project_root)]


def dispatch_review(
    project_root: Path,
    pr_number: int,
    *,
    surface_name: str = "auto",
    post_comment: bool = False,
    runtime_name: str | None = None,
) -> str:
    """Spawn `agent review` on a visible surface; return a 'where it went' string.

    Unlike `agent dispatch` there is no task worktree to attach to — a review
    is read-only and runs against the project root — so the run is shown on the
    project's own card.
    """
    chosen = surfaces.pick(surface_name)
    command = review_command(
        project_root, pr_number, post_comment=post_comment, runtime_name=runtime_name
    )
    return chosen.spawn(f"agent-review-pr-{pr_number}", command, project_root)
