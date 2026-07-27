from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_ops import github, worktree
from agent_ops.config import load_project_config
from agent_ops.fallback import run_with_fallback
from agent_ops.prompts import render_task
from agent_ops.utils import SLOW_GIT_TIMEOUT_S, CommandError, run
from agent_ops.workflows.implement import role_request
from agent_ops.workflows.triage import LABEL_COLORS

_RESULT_LINE = re.compile(r"^#(\d+)\s*[—-]+\s*(.+)$")

#: How much repo-authored focus text may reach the prompt. Repo text is
#: trusted at the same level as AGENTS.md, but a long one would still crowd
#: out the filing rules it sits above, so it is bounded rather than trusted
#: to be short.
FOCUS_MAX_CHARS = 2000


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


def focus_block(focus: str) -> str:
    """The `{repo_focus}` section of the scout prompt, or "" when unconfigured.

    Same shape as `skills.load_skills`: the workflow builds the block and the
    template just holds the placeholder. A blank focus renders the prompt
    byte-identically to a repo that never configured one, which is why the
    block carries its own surrounding newlines and the template line does not.

    The block sits below the "ground every candidate in a signal" rule and
    above `## Filing`, and closes by pointing at the rules that follow: a repo
    may say what to look for, never what scout is allowed to file.
    """
    text = focus.strip()
    if not text:
        return ""
    if len(text) > FOCUS_MAX_CHARS:
        text = text[:FOCUS_MAX_CHARS].rstrip() + " […truncated]"
    return (
        "\n## Repo focus\n\n"
        f"{text}\n\n"
        "Candidates from this section still need a concrete signal, and the\n"
        "caps, duplicate check and danger-zone ban below still apply.\n"
    )


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
    try:
        sync = github.sync_labels(
            project_root,
            {name: LABEL_COLORS[name] for name in ("backlog", "proposed-by-agent")},
            repo=github.remote_slug(project_root),
        )
    except CommandError as exc:
        log(f"could not sync labels: {exc}")
    else:
        for name, reason in sync.failed:
            log(f"could not sync label {name}: {reason}")

    # Scout against the WORKING branch — a TODO already resolved on staging
    # must not become an issue.
    run(
        ["git", "fetch", "origin", config.base_branch],
        cwd=project_root,
        timeout=SLOW_GIT_TIMEOUT_S,
    )
    scout_wt = worktree.create_detached(
        project_root, config.worktree_dir, "scout-tmp", f"origin/{config.base_branch}"
    )
    try:
        runtime, request = role_request(
            config,
            "planner",
            render_task(
                "scout",
                max_issues=str(max_issues),
                repo_focus=focus_block(config.scout.focus),
            ),
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
        result = run_with_fallback(runtime, request, on_event=log)
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
