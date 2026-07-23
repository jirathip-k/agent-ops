from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent_ops import github, worktree
from agent_ops.config import ProjectConfig, load_project_config
from agent_ops.loop import run_task_loop
from agent_ops.prompts import render_task
from agent_ops.runtimes import RunRequest, Runtime, get_runtime
from agent_ops.skills import load_skills
from agent_ops.utils import run


def run_implement(
    project_root: Path,
    issue_number: int,
    *,
    runtime_name: str | None = None,
    open_pr: bool = True,
    keep_worktree: bool = False,
    log: Callable[[str], None] = print,
) -> bool:
    """Issue → worktree → implement loop → self-review → commit → push → PR.

    Returns True on success. On failure the worktree is kept for inspection.
    """
    config = load_project_config(project_root)
    runtime = get_runtime(runtime_name or config.runtime.name)
    if not runtime.available():
        raise RuntimeError(f"Runtime {runtime.name!r} CLI is not installed/on PATH")

    issue = github.get_issue(issue_number, cwd=project_root)
    task_id = f"issue-{issue_number}"
    branch = f"fix/{task_id}"

    log(f"creating worktree for {branch} from {config.base_branch}")
    wt_path = worktree.create(
        project_root, config.worktree_dir, task_id, branch, config.base_branch
    )

    skills_text = load_skills(config.skills, project_root)
    labels = ", ".join(lbl["name"] for lbl in issue.get("labels", [])) or "none"
    prompt = render_task(
        "implement",
        issue_number=str(issue["number"]),
        issue_title=issue["title"],
        issue_body=issue.get("body") or "(no description)",
        issue_labels=labels,
        branch=branch,
        skills=skills_text,
    )
    request = RunRequest(
        prompt=prompt,
        cwd=wt_path,
        model=config.runtime.model,
        max_turns=config.runtime.max_turns,
        permission_mode=config.runtime.permission_mode,
    )

    outcome = run_task_loop(runtime, request, config, wt_path, on_event=log)
    if not outcome.ok:
        log(
            f"FAILED after {outcome.attempts} attempts; worktree kept at {wt_path} "
            f"for inspection. Failing gates: {', '.join(g.name for g in outcome.gate_failures)}"
        )
        return False

    if config.loop.self_review and not _self_review_ok(runtime, config, wt_path, log=log):
        log(f"self-review requested changes; worktree kept at {wt_path}")
        return False

    diff_stat = run(["git", "diff", "--stat"], cwd=wt_path).stdout.strip()
    log(f"changes:\n{diff_stat}")

    title = f"fix: {issue['title']} (#{issue_number})"
    run(["git", "add", "-A"], cwd=wt_path)
    run(["git", "commit", "-m", title], cwd=wt_path)

    if open_pr:
        run(["git", "push", "-u", "origin", branch], cwd=wt_path)
        body = (
            f"Closes #{issue_number}.\n\n"
            f"Automated implementation via agent-ops "
            f"({runtime.name}, {outcome.attempts} attempt(s), gates passed)."
        )
        url = github.create_pr(wt_path, base=config.base_branch, title=title, body=body)
        log(f"opened PR: {url}")

    if not keep_worktree:
        worktree.remove(project_root, config.worktree_dir, task_id, force=True)
        log("worktree removed (branch kept)")
    return True


def _self_review_ok(
    runtime: Runtime,
    config: ProjectConfig,
    wt_path: Path,
    *,
    log: Callable[[str], None],
) -> bool:
    diff = run(["git", "diff"], cwd=wt_path).stdout
    if not diff.strip():
        log("self-review skipped: empty diff")
        return False
    prompt = render_task("review", diff=diff, context="Pre-commit self-review of local changes.")
    result = runtime.run(
        RunRequest(prompt=prompt, cwd=wt_path, permission_mode="plan", model=config.runtime.model)
    )
    verdict_ok = result.ok and "APPROVE" in result.text.upper().split("REQUEST CHANGES")[0]
    log(f"self-review verdict: {'APPROVE' if verdict_ok else 'REQUEST CHANGES'}")
    if not verdict_ok:
        log(result.text)
    return verdict_ok
