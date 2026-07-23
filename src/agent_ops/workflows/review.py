from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent_ops import github
from agent_ops.config import load_project_config
from agent_ops.prompts import render_task
from agent_ops.runtimes import RunRequest, get_runtime


def run_review(
    project_root: Path,
    pr_number: int,
    *,
    runtime_name: str | None = None,
    post_comment: bool = False,
    log: Callable[[str], None] = print,
) -> str:
    """Run a read-only review agent over a PR diff; optionally post the result."""
    config = load_project_config(project_root)
    runtime = get_runtime(runtime_name or config.runtime.name)
    if not runtime.available():
        raise RuntimeError(f"Runtime {runtime.name!r} CLI is not installed/on PATH")

    pr = github.pr_view(pr_number, cwd=project_root)
    diff = github.pr_diff(pr_number, cwd=project_root)
    prompt = render_task(
        "review",
        diff=diff,
        context=f"PR #{pr['number']}: {pr['title']}\n\n{pr.get('body') or ''}",
    )

    result = runtime.run(
        RunRequest(
            prompt=prompt, cwd=project_root, permission_mode="plan", model=config.runtime.model
        )
    )
    if not result.ok:
        raise RuntimeError(f"Review run failed: {result.text}")

    if post_comment:
        github.comment_on_pr(pr_number, f"## Agent review\n\n{result.text}", cwd=project_root)
        log(f"posted review comment on PR #{pr_number}")
    return result.text
