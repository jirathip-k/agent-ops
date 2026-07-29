from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_ops import claims, github, messages, worktree
from agent_ops.config import load_project_config
from agent_ops.fallback import run_with_fallback
from agent_ops.github import Label
from agent_ops.prompts import render_task
from agent_ops.utils import SLOW_GIT_TIMEOUT_S, CommandError, run
from agent_ops.workflows.implement import role_request

# An issue is settled -- skipped by future runs -- once it carries ANY
# bucket label, regardless of `triage:done`. A bucket label is authoritative
# no matter who applied it or when: a human's go-ahead (docs/workflow.md), a
# human's `needs-human`, and every issue this lane already bucketed are all
# settled the same way. The model classifying issues below never sees an
# issue's existing labels -- only number, title and body reach the prompt
# (see `issues_text` below) -- so it cannot be trusted to honor one it can't
# see: any rule that depends on an issue's existing labels has to be
# enforced here, in the query, never in the prompt. (A prior version of this
# guard required `triage:done` alongside the bucket, which let a bucket a
# human applied by hand -- or one this lane applied before `triage:done`
# existed -- fall through to be reclassified and, worst case, dispatched
# out from under a `needs-human` hold; see #257.)
#
# `triage:done` with no bucket is the one case NOT settled: it means
# Housekeeping stamped the issue without ever bucketing it (a legacy issue
# from before that pairing was guaranteed, prompts/tasks/triage.md, #257, or
# one that would exist if it ever regressed) -- picked back up and bucketed
# normally rather than left orphaned.
BUCKET_LABELS = {"agent-ready", "needs-human", "backlog"}

# Stamped by both surfaces on every processed issue (see the comment above).
# Named here, not just inline where it's used, so `agent status --pipeline`
# can list it as a pipeline stage without retyping the string, and so
# LABEL_COLORS can sync it before either surface ever writes it.
TRIAGE_DONE_LABEL = "triage:done"

LABEL_COLORS: dict[str, Label] = {
    "agent-ready": Label("1d76db", "Groomed and safe for an agent to implement"),
    "needs-human": Label("d93f0b", "Triage could not classify this without a person"),
    "backlog": Label("c5def5", "Idea without acceptance criteria yet — not actionable"),
    "found-by-audit": Label("fbca04", "Filed by an agent auditing the codebase, never fixed by it"),
    "proposed-by-agent": Label("bfd4f2", "Filed by the scout lane from a mined signal"),
    TRIAGE_DONE_LABEL: Label(
        "ededed", "Triage has processed this issue — bucketed, or handled without one"
    ),
}

# The labels the spec/plan CI lanes select on. Not verdict labels for triage,
# but groom may now emit them (#97), so they live here rather than in the CLI:
# a lane that applies a label has to be able to create it first.
GATE_LABELS: dict[str, Label] = {
    "spec-requested": Label(
        "5319e7", "Requests the spec lane: turn this issue into acceptance criteria"
    ),
    "plan-requested": Label(
        "1d76db", "Requests the plan lane: post an implementation plan on this issue"
    ),
}

_RESULT_LINE = re.compile(r"^#(\d+)\s+(agent-ready|needs-human|backlog)\s*[—-]+\s*(.+)$")

# Mirrors the pinning prefix-check in workflows/implement.py's `_is_pinned`.
_SPEC_PLAN_PREFIXES = ("## Agent spec", "## Agent plan")
# Bounds prompt cost (#267): only the newest spec/plan comment reaches the
# prompt, and only up to this many characters of it.
_SPEC_PLAN_COMMENT_MAX_CHARS = 4000


def latest_spec_or_plan_comment(issue: dict[str, Any]) -> str | None:
    """The newest `## Agent spec` / `## Agent plan` comment on `issue`, if any.

    Triage/groom used to classify from title+body alone, which re-judged (and
    could strip `agent-ready` from) an issue whose body is thin precisely
    because its substance lives in a spec/plan comment instead (#267). Only
    the single newest match reaches the caller, truncated -- not the full
    thread -- to bound prompt cost.

    Comments are untrusted data like any other GitHub text
    (docs/trust-model.md): this feeds the readiness *assessment* only. It
    cannot assert `agent-ready` into existence by merely claiming it, and it
    can never authorize a danger-zone change on its own.
    """
    comments = issue.get("comments") or []
    for comment in reversed(comments):
        body = (comment.get("body") or "").lstrip()
        if body.startswith(_SPEC_PLAN_PREFIXES):
            return body[:_SPEC_PLAN_COMMENT_MAX_CHARS]
    return None


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
            "number,title,body,labels,comments",
        ],
        cwd=project_root,
    )
    # `agent:claimed` means a run is actively implementing this issue right
    # now (see claims.py) -- a triage pass must never stamp it `needs-human` +
    # `triage:done` underneath that run, so it is skipped outright rather
    # than handed to the prompt's stale-claim check, which this surface lacks
    # the tools (`gh api ... /events`, `gh issue edit`) to act on anyway.
    always_skip = {claims.CLAIM_LABEL}
    issues = [
        i
        for i in json.loads(proc.stdout)
        if not (labels := {lbl["name"] for lbl in i.get("labels", [])}) & always_skip
        and not (BUCKET_LABELS & labels)
    ]
    if not issues:
        log("nothing to triage — every open issue is already classified")
        return []
    log(f"triaging {len(issues)} issue(s)")

    def _issue_block(i: dict[str, Any]) -> str:
        spec = latest_spec_or_plan_comment(i)
        return (
            f"### #{i['number']}: {i['title']}\n{i.get('body') or '(no description)'}\n\n"
            f"### Spec/plan on file\n{spec if spec is not None else '(none)'}"
        )

    issues_text = "\n\n".join(_issue_block(i) for i in issues)

    # Classify against the WORKING branch (staging), not the local checkout —
    # the checkout may sit on a stale main while merged work lives on staging.
    run(
        ["git", "fetch", "origin", config.base_branch],
        cwd=project_root,
        timeout=SLOW_GIT_TIMEOUT_S,
    )
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
        result = run_with_fallback(runtime, request, on_event=log)
    finally:
        worktree.remove(project_root, config.worktree_dir, "triage-tmp", force=True)
    if not result.ok:
        raise RuntimeError(f"Triage run failed: {result.text}")

    results = parse_triage(result.text)
    if not results:
        raise RuntimeError(f"Triage produced no parseable results:\n{result.text[-500:]}")

    try:
        sync = github.sync_labels(project_root, LABEL_COLORS, repo=github.remote_slug(project_root))
    except CommandError as exc:
        log(f"could not sync labels: {exc}")
    else:
        for name, reason in sync.failed:
            log(f"could not sync label {name}: {reason}")

    for r in results:
        run(
            [
                "gh",
                "issue",
                "edit",
                str(r.number),
                "--add-label",
                r.verdict,
                "--add-label",
                TRIAGE_DONE_LABEL,
            ],
            cwd=project_root,
        )
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
                spawned = surfaces.pick("auto").spawn(
                    f"agent-issue-{r.number}",
                    ["agent", "implement", str(r.number), "--project", str(project_root)],
                    project_root,
                )
                # Recorded for the same reason `agent dispatch` records it:
                # these runs are exactly what a later `agent runs --wait`
                # watches, so they need an address too (issue #98).
                messages.record_spawn(
                    project_root,
                    r.number,
                    surface=spawned.surface,
                    handle=spawned.handle,
                    pid=spawned.pid,
                    log_path=spawned.log_path,
                    log=log,
                )
                log(f"#{r.number} dispatched → {spawned.where}")
    return results
