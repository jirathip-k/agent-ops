from __future__ import annotations

import json
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
from agent_ops.workflows.triage import BUCKET_LABELS, GATE_LABELS, LABEL_COLORS

# Verdicts that route an issue into a CI lane instead of bucketing it (#97).
# They say "not ready to implement", so they clear `agent-ready` — but they
# leave the other buckets alone: an issue keeps its bucket while it waits in
# the spec/plan queue, and stripping it would only feed it back to triage.
GATE_VERDICTS = frozenset(GATE_LABELS)

VERDICTS = BUCKET_LABELS | GATE_VERDICTS | {"close-fixed", "close-invalid", "keep"}

# Deliberately not an alternation of the known verdicts: a model that invents
# one should be visible in the log and skipped, not silently dropped by the
# regex — dropping every line is indistinguishable from an unparseable run.
_RESULT_LINE = re.compile(r"^#(\d+)\s+([a-z][a-z-]*)\s*[—-]+\s*(.+)$")


@dataclass(frozen=True)
class GroomResult:
    number: int
    verdict: str
    reason: str


def parse_groom(text: str) -> list[GroomResult]:
    """Parse the GROOM RESULTS block from the agent's final message."""
    _, marker, tail = text.rpartition("GROOM RESULTS:")
    if not marker:
        return []
    results = []
    for line in tail.strip().splitlines():
        m = _RESULT_LINE.match(line.strip())
        if m:
            results.append(GroomResult(int(m.group(1)), m.group(2), m.group(3).strip()))
    return results


def run_groom(project_root: Path, *, log: Callable[[str], None] = print) -> list[GroomResult]:
    """Re-validate every open issue against the current working branch.

    Closes verifiably-fixed and invalid issues (with an evidence comment),
    promotes workable ones to agent-ready, routes the ones that need design
    or elaboration into the plan/spec lanes, and refreshes stale buckets.
    Dispatch and merge remain the human's — groom only maintains the queue.
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
            "100",
            "--json",
            "number,title,body,labels,updatedAt",
        ],
        cwd=project_root,
    )
    issues = json.loads(proc.stdout)
    if not issues:
        log("nothing to groom — no open issues")
        return []
    log(f"grooming {len(issues)} issue(s)")

    issues_text = "\n\n".join(
        f"### #{i['number']}: {i['title']}\n"
        f"labels: {', '.join(lbl['name'] for lbl in i.get('labels', [])) or '(none)'} | "
        f"updated: {i['updatedAt']}\n"
        f"{i.get('body') or '(no description)'}"
        for i in issues
    )
    labels_by_number = {i["number"]: {lbl["name"] for lbl in i.get("labels", [])} for i in issues}

    # Groom against the WORKING branch — merged-but-unpromoted fixes live there.
    run(
        ["git", "fetch", "origin", config.base_branch],
        cwd=project_root,
        timeout=SLOW_GIT_TIMEOUT_S,
    )
    groom_wt = worktree.create_detached(
        project_root, config.worktree_dir, "groom-tmp", f"origin/{config.base_branch}"
    )
    try:
        runtime, request = role_request(
            config,
            "planner",
            render_task("groom", issues=issues_text),
            groom_wt,
            # read-only gh: verify fixed/duplicate claims against merged PRs
            extra_allowed_tools=(
                "Bash(gh issue list:*)",
                "Bash(gh issue view:*)",
                "Bash(gh search issues:*)",
                "Bash(gh pr list:*)",
                "Bash(gh pr view:*)",
            ),
        )
        result = run_with_fallback(runtime, request, on_event=log)
    finally:
        worktree.remove(project_root, config.worktree_dir, "groom-tmp", force=True)
    if not result.ok:
        raise RuntimeError(f"Groom run failed: {result.text}")

    results = parse_groom(result.text)
    if not results:
        raise RuntimeError(f"Groom produced no parseable results:\n{result.text[-500:]}")

    # Gate labels included: groom can now apply them, and `gh issue edit
    # --add-label` fails outright against a label the repo never created.
    try:
        sync = github.sync_labels(
            project_root, LABEL_COLORS | GATE_LABELS, repo=github.remote_slug(project_root)
        )
    except (CommandError, OSError) as exc:
        log(f"could not sync labels: {exc}")
    else:
        for name, reason in sync.failed:
            log(f"could not sync label {name}: {reason}")

    for r in results:
        current = labels_by_number.get(r.number, set())
        if r.verdict not in VERDICTS:
            # A verdict nobody defined is a no-op, never a crash: applying it
            # would mean creating a label the agent invented.
            log(f"#{r.number} skipped — unrecognised verdict {r.verdict!r}: {r.reason}")
            continue
        if r.verdict == "keep":
            log(f"#{r.number} keep: {r.reason}")
            continue
        if r.verdict in ("close-fixed", "close-invalid"):
            run(
                [
                    "gh",
                    "issue",
                    "close",
                    str(r.number),
                    "--comment",
                    f"**Groom: {r.verdict}** — {r.reason}",
                ],
                cwd=project_root,
            )
            log(f"#{r.number} closed ({r.verdict}): {r.reason}")
            continue
        edit = ["gh", "issue", "edit", str(r.number), "--add-label", r.verdict]
        superseded = {"agent-ready"} if r.verdict in GATE_VERDICTS else BUCKET_LABELS
        for stale in (superseded - {r.verdict}) & current:
            edit += ["--remove-label", stale]
        run(edit, cwd=project_root)
        if r.verdict not in current:
            run(
                [
                    "gh",
                    "issue",
                    "comment",
                    str(r.number),
                    "--body",
                    f"**Groom: {r.verdict}** — {r.reason}",
                ],
                cwd=project_root,
            )
        log(f"#{r.number} → {r.verdict}: {r.reason}")
    return results
