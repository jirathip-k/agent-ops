from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_ops import github, worktree
from agent_ops.config import load_project_config
from agent_ops.fallback import artifact_footer, run_with_fallback
from agent_ops.prompts import escalated, opens_with_escalation_word, render_task
from agent_ops.runtimes import RunRequest, RunResult
from agent_ops.utils import SLOW_GIT_TIMEOUT_S, CommandError, run
from agent_ops.workflows.implement import _labels, role_request

# Header on the comment an escalation leaves behind, and the marker that stops
# a re-run from posting a second one. Deliberately NOT a `## Agent spec` prefix:
# that one is pinned into the planner's prompt as a spec to build on
# (`implement._PINNED_PREFIXES`), and an escalation is the opposite of one.
ESCALATION_HEADER = "## Spec agent — escalation"


def _already_escalated(issue: dict[str, Any]) -> bool:
    """True if a previous run already left its escalation on this issue.

    The gate label only clears on success, so an escalating issue is specced
    again on every scheduled run. The question a human has to answer is the
    same one each time — post it once, not once a night.
    """
    return any(
        (comment.get("body") or "").lstrip().startswith(ESCALATION_HEADER)
        for comment in issue.get("comments") or []
    )


def _post_escalation(
    project_root: Path,
    issue: dict[str, Any],
    issue_number: int,
    request: RunRequest,
    result: RunResult,
    log: Callable[[str], None],
) -> None:
    """Put the agent's reasoning on the issue, where a human will actually find it.

    An escalation only ever reached the run's log, and the CI lane collapses
    that into `Spec failed for issue(s): N` — so the question the agent asked
    was discarded along with the run (#129). Best-effort throughout: this runs
    on the way to raising, and a `gh` that is missing or unhappy must not
    replace the escalation with a different error.
    """
    if _already_escalated(issue):
        log(f"escalation already posted on issue #{issue_number}; not posting again")
        return
    body = (
        f"{ESCALATION_HEADER}\n\n"
        "The spec agent stopped rather than writing a spec:\n\n"
        f"{result.text}{artifact_footer(request, result)}"
    )
    try:
        github.comment_on_issue(issue_number, body, cwd=project_root)
    except CommandError as exc:
        log(f"could not post escalation on issue #{issue_number}: {exc}")
        return
    log(f"posted escalation on issue #{issue_number}")


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
    the spec agent found a question only a human can answer — after leaving
    that question on the issue for them to answer.
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
    run(
        ["git", "fetch", "origin", config.base_branch],
        cwd=project_root,
        timeout=SLOW_GIT_TIMEOUT_S,
    )
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
        result = run_with_fallback(runtime, request, on_event=log)
    finally:
        worktree.remove(project_root, config.worktree_dir, task_id, force=True)
    if not result.ok:
        raise RuntimeError(f"Spec run failed: {result.text}")
    if escalated(result.text):
        if post:
            _post_escalation(project_root, issue, issue_number, request, result, log)
        raise RuntimeError(f"Spec agent escalated:\n{result.text}")
    if opens_with_escalation_word(result.text):
        log(
            "note: the spec opens with the word ESCALATE but not as the sentinel "
            "(`ESCALATE:` or a line of its own) — treating it as a spec"
        )

    if post:
        body = f"## Agent spec\n\n{result.text}{artifact_footer(request, result)}"
        github.comment_on_issue(issue_number, body, cwd=project_root)
        log(f"posted spec on issue #{issue_number}")
    return result.text
