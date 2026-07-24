from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_ops import github, orca, worktree
from agent_ops.config import ProjectConfig, load_project_config
from agent_ops.loop import run_task_loop
from agent_ops.prompts import render_task
from agent_ops.runtimes import RunRequest, Runtime, get_runtime
from agent_ops.skills import load_skills
from agent_ops.utils import CommandError, run

NO_PLAN_TEXT = "(no planning stage — analyze the root cause yourself before editing)"


def gate_allowed_tools(config: ProjectConfig) -> tuple[str, ...]:
    """Permission patterns pre-approving the project's gate commands.

    Headless runs have nobody to answer permission prompts, so the implementer
    must be able to run test/lint/typecheck itself. Compound commands
    (a && b) are split because permissions are checked per component.
    """
    patterns: list[str] = []
    for name in ("setup", "test", "lint", "typecheck"):
        command = getattr(config.commands, name, None)
        if not command:
            continue
        for part in command.split("&&"):
            part = part.strip()
            if part:
                patterns += [f"Bash({part})", f"Bash({part}:*)"]
    return tuple(dict.fromkeys(patterns))


def role_request(
    config: ProjectConfig,
    role_name: str,
    prompt: str,
    cwd: Path,
    *,
    runtime_override: str | None = None,
    extra_allowed_tools: tuple[str, ...] = (),
) -> tuple[Runtime, RunRequest]:
    """Resolve a role (planner/implementer/reviewer) to its runtime and request."""
    role = config.resolve_role(role_name)
    runtime = get_runtime(runtime_override or role.runtime)
    if not runtime.available():
        raise RuntimeError(f"Runtime {runtime.name!r} CLI is not installed/on PATH")
    request = RunRequest(
        prompt=prompt,
        cwd=cwd,
        model=role.model,
        max_turns=role.max_turns,
        permission_mode=role.permission_mode,
        stream=config.runtime.stream,
        # every role may run the gates: implementer to iterate, planner to
        # reproduce, reviewer to verify — write access still differs by mode
        allowed_tools=gate_allowed_tools(config) + extra_allowed_tools,
    )
    return runtime, request


def make_plan(
    config: ProjectConfig,
    issue: dict[str, Any],
    cwd: Path,
    *,
    runtime_override: str | None = None,
) -> str:
    """Run the planner role. Returns the plan text; raises on ESCALATE or failure."""
    prompt = render_task(
        "plan",
        issue_number=str(issue["number"]),
        issue_title=issue["title"],
        issue_body=issue.get("body") or "(no description)",
        issue_labels=_labels(issue),
    )
    runtime, request = role_request(
        config, "planner", prompt, cwd, runtime_override=runtime_override
    )
    result = runtime.run(request)
    if not result.ok:
        raise RuntimeError(f"Planner run failed: {result.text}")
    if result.text.lstrip().upper().startswith("ESCALATE"):
        raise RuntimeError(f"Planner escalated:\n{result.text}")
    return result.text


def run_implement(
    project_root: Path,
    issue_number: int,
    *,
    runtime_name: str | None = None,
    open_pr: bool = True,
    keep_worktree: bool = False,
    plan_file: Path | None = None,
    log: Callable[[str], None] = print,
) -> bool:
    """Issue → worktree → plan (smart model) → implement loop → self-review → PR.

    Each stage runs as a separate agent with fresh context: the planner and
    reviewer roles default to a stronger model in read-only mode, the
    implementer does the bulk work (see `agents:` in config). Returns True on
    success. On implement/review failure the worktree is kept for inspection.
    """
    config = load_project_config(project_root)
    issue = github.get_issue(issue_number, cwd=project_root)
    task_id = f"issue-{issue_number}"
    branch = f"fix/{task_id}"

    log(f"creating worktree for {branch} from {config.base_branch}")
    wt_path = worktree.create(
        project_root, config.worktree_dir, task_id, branch, config.base_branch
    )
    orca.report(wt_path, comment=f"#{issue_number}: setting up", status=orca.STATUS_IN_PROGRESS)

    if config.commands.setup:
        log(f"setup: {config.commands.setup}")
        try:
            run(["sh", "-c", config.commands.setup], cwd=wt_path)
        except CommandError as exc:
            log(f"setup failed: {exc}")
            _abort_cleanly(project_root, config, task_id, log)
            return False

    plan = NO_PLAN_TEXT
    if plan_file is not None:
        # human-approved plan (e.g. from a prior escalation) — skip the planner
        plan = plan_file.read_text()
        log(f"using approved plan from {plan_file} ({len(plan.splitlines())} lines)")
    elif config.loop.plan:
        planner_role = config.resolve_role("planner")
        log(f"planning (model: {planner_role.model or 'default'})")
        orca.report(wt_path, comment=f"#{issue_number}: planning")
        try:
            plan = make_plan(config, issue, wt_path, runtime_override=runtime_name)
        except RuntimeError as exc:
            log(str(exc))
            _abort_cleanly(project_root, config, task_id, log)
            log("issue needs a human decision")
            return False
        log(f"plan ready ({len(plan.splitlines())} lines)")

    prompt = render_task(
        "implement",
        issue_number=str(issue["number"]),
        issue_title=issue["title"],
        issue_body=issue.get("body") or "(no description)",
        issue_labels=_labels(issue),
        branch=branch,
        plan=plan,
        skills=load_skills(config.skills, project_root),
    )
    runtime, request = role_request(
        config, "implementer", prompt, wt_path, runtime_override=runtime_name
    )

    orca.report(wt_path, comment=f"#{issue_number}: implementing")
    outcome = run_task_loop(runtime, request, config, wt_path, on_event=log)
    if not outcome.ok:
        failing = ", ".join(g.name for g in outcome.gate_failures)
        log(
            f"FAILED after {outcome.attempts} attempts; worktree kept at {wt_path} "
            f"for inspection. Failing gates: {failing}"
        )
        orca.report(wt_path, comment=f"#{issue_number}: FAILED gates ({failing}); worktree kept")
        return False

    if config.loop.self_review and not _self_review_ok(
        config, wt_path, log=log, runtime_override=runtime_name
    ):
        log(f"self-review requested changes; worktree kept at {wt_path}")
        orca.report(wt_path, comment=f"#{issue_number}: self-review requested changes")
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
        orca.report(
            wt_path, comment=f"#{issue_number}: PR opened {url}", status=orca.STATUS_IN_REVIEW
        )
        if config.loop.auto_merge:
            from agent_ops.workflows.merge import run_merge

            pr_number = int(url.rstrip("/").rsplit("/", 1)[-1])
            log("auto-merge enabled — applying merge rules")
            # never overrides: a blocked PR stays open for a human
            run_merge(project_root, pr_number, log=log)

    if not keep_worktree:
        worktree.remove(project_root, config.worktree_dir, task_id, force=True)
        log("worktree removed (branch kept)")
    return True


def _abort_cleanly(
    project_root: Path,
    config: ProjectConfig,
    task_id: str,
    log: Callable[[str], None],
) -> None:
    """Remove worktree AND its branch after an abort where nothing was committed.

    Leaving the branch behind makes every re-run fail with
    'a branch named fix/<task> already exists'.
    """
    worktree.remove(project_root, config.worktree_dir, task_id, force=True, delete_branch=True)
    log("worktree and branch removed (nothing was changed)")


def _labels(issue: dict[str, Any]) -> str:
    return ", ".join(lbl["name"] for lbl in issue.get("labels", [])) or "none"


def _self_review_ok(
    config: ProjectConfig,
    wt_path: Path,
    *,
    log: Callable[[str], None],
    runtime_override: str | None = None,
) -> bool:
    diff = run(["git", "diff"], cwd=wt_path).stdout
    if not diff.strip():
        log("self-review skipped: empty diff")
        return False
    prompt = render_task("review", diff=diff, context="Pre-commit self-review of local changes.")
    runtime, request = role_request(
        config, "reviewer", prompt, wt_path, runtime_override=runtime_override
    )
    result = runtime.run(request)
    verdict_ok = result.ok and "APPROVE" in result.text.upper().split("REQUEST CHANGES")[0]
    log(f"self-review verdict: {'APPROVE' if verdict_ok else 'REQUEST CHANGES'}")
    if not verdict_ok:
        log(result.text)
    return verdict_ok
