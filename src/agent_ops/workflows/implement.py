from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_ops import claims, github, grants, messages, orca, runs, surfaces, worktree
from agent_ops.config import ProjectConfig, load_project_config
from agent_ops.fallback import model_note, run_with_fallback
from agent_ops.loop import LoopOutcome, run_task_loop
from agent_ops.prompts import escalated, opens_with_escalation_word, render_task, verdict_of
from agent_ops.runtimes import RunRequest, RunResult, Runtime, get_runtime
from agent_ops.skills import load_skills
from agent_ops.utils import SLOW_GIT_TIMEOUT_S, CommandError, flush_print, run

NO_PLAN_TEXT = "(no planning stage — analyze the root cause yourself before editing)"


def task_identifiers(issue_number: int) -> tuple[str, str]:
    """The (task_id, branch) pair every stage derives from an issue number.

    Dispatch pre-creates the worktree with these names and implement reuses
    it, so the naming must live in exactly one place.
    """
    task_id = f"issue-{issue_number}"
    return task_id, f"fix/{task_id}"


def _feedback_path(project_root: Path, issue_number: int) -> Path:
    """Where a self-review halt's findings are stashed for `agent resume` to pick up.

    Lives under the project root, never the worktree: `.agent-runs/` is the
    existing convention for run artifacts, and a file inside the worktree
    risks being swept up by the implementer's own `git add -A`.
    """
    return project_root / ".agent-runs" / f"issue-{issue_number}-feedback.md"


def _ad_hoc_message_path(project_root: Path, issue_number: int) -> Path:
    """Where `--message` text is staged, deliberately NOT `_feedback_path`.

    That file is the halt record. Writing an ad-hoc note over it would destroy
    the self-review findings permanently, and a later bare `agent resume` would
    replay the note instead of the review it was meant to address.
    """
    return project_root / ".agent-runs" / f"issue-{issue_number}-resume-message.md"


def _resolve_grant(
    project_root: Path,
    issue_number: int,
    grant_file: Path | None,
    *,
    log: Callable[[str], None],
) -> grants.Grant | None:
    """This cycle's danger-zone authorization: `--grant-file`, else whatever an
    earlier cycle on this issue persisted (issue #200).

    An explicit `--grant-file` is persisted immediately, overwriting anything
    from before, so it survives every `agent resume` on this issue without
    having to be repeated — the file is the only channel a grant has once the
    worktree exists. `_finish_run` is what clears it, once the run it
    authorized actually lands.
    """
    if grant_file is not None:
        grant = grants.load(grant_file, issue=issue_number)
        grants.persist(project_root, grant)
        log(f"authorization loaded: {grant.granted_by} — {grant.scope}")
        return grant
    grant = grants.load_persisted(project_root, issue_number)
    if grant is not None:
        log(f"authorization carried over from an earlier cycle: {grant.granted_by} — {grant.scope}")
    return grant


def _render_authorization(grant: grants.Grant | None) -> str:
    """The `{authorization}` field for the implement/resume prompts."""
    if grant is None:
        return "(none — danger-zone rules fully in force)"
    paths = "\n".join(f"- `{p}`" for p in grant.paths)
    expiry = f"\nExpires: {grant.expires}" if grant.expires is not None else ""
    return (
        f"Granted by **{grant.granted_by}**, for this issue only.{expiry}\n\n"
        f"Scope: {grant.scope}\n\n"
        f"Only these paths are exempted from the danger-zone rule below — stay strictly "
        f"inside them:\n{paths}"
    )


def _authorization_pr_section(grant: grants.Grant) -> str:
    """The `## Authorization` block `_finish_run` appends to a granted run's PR body."""
    paths = "\n".join(f"- `{p}`" for p in grant.paths)
    expiry = f"\n- Expires: {grant.expires}" if grant.expires is not None else ""
    return (
        "\n\n## Authorization\n\n"
        f"- Granted by: **{grant.granted_by}**{expiry}\n"
        f"- Scope: {grant.scope}\n"
        f"- Paths:\n{paths}\n"
    )


def _existing_worktree(
    project_root: Path,
    config: ProjectConfig,
    issue_number: int,
    *,
    log: Callable[[str], None] = lambda _: None,
) -> Path:
    """The task worktree a prior `agent dispatch`/`implement` left behind — recovered from
    the branch if the worktree itself is gone — or a clear error.

    A halted run's worktree is disposable (cleaned up by hand, wiped disk) but
    its branch — `fix/issue-N` — is the durable record of the work, locally
    and/or on the remote. When the worktree is missing this recreates it from
    that branch instead of telling the operator to `agent dispatch`, which
    would build a fresh worktree from `base_branch` and, on an issue with an
    open PR, only offers `--force` — starting a competing duplicate
    implementation.

    Recovery never resets or discards commits: it only ever creates a new
    worktree on the existing branch and, when the local branch was strictly
    behind the remote, fast-forwards it (`merge --ff-only`, never `reset
    --hard`). A branch with commits on both sides refuses rather than
    guessing which one to keep — the one case manual git remains the honest
    answer for.
    """
    task_id, branch = task_identifiers(issue_number)
    wt_path = project_root / config.worktree_dir / task_id
    branches = {wt.branch for wt in worktree.list_worktrees(project_root)}
    if wt_path.is_dir() and branch in branches:
        return wt_path  # today's happy path — no fetch, no network call

    status = worktree.remote_branch_status(project_root, branch)
    if not status.local and not status.remote:
        raise FileNotFoundError(
            f"no worktree for issue #{issue_number} at {wt_path} — "
            f"run `agent dispatch {issue_number}` to start one"
        )
    if status.diverged:
        raise CommandError(
            f"branch {branch!r} has diverged from origin/{branch} "
            f"({status.ahead} commit(s) ahead, {status.behind} behind) — the worktree for "
            f"issue #{issue_number} is gone and recovery cannot pick a side. Resolve manually "
            f"(e.g. `git worktree add {wt_path} {branch}` then rebase or merge) and re-run "
            "`agent resume`."
        )

    worktree.prune(project_root)
    recovered = worktree.create_tracking(project_root, config.worktree_dir, task_id, branch)
    if status.remote and status.behind > 0:
        run(
            ["git", "merge", "--ff-only", f"origin/{branch}"],
            cwd=recovered,
            timeout=SLOW_GIT_TIMEOUT_S,
        )
        log(
            f"recovered: recreated worktree at {recovered} on existing branch {branch} "
            f"(from origin/{branch}, fast-forwarded {status.behind} commit(s))"
        )
    elif status.remote:
        log(
            f"recovered: recreated worktree at {recovered} on existing branch {branch} "
            f"(from origin/{branch})"
        )
    else:
        log(
            f"recovered: recreated worktree at {recovered} on existing branch {branch} — "
            "this branch has not been pushed; push it before this machine is lost"
        )
    return recovered


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
    """Resolve a role (planner/implementer/reviewer) to its runtime and request.

    The override reaches `resolve_role` so the model tier is looked up in the
    table of the runtime that will actually run — otherwise `--runtime codex`
    hands codex a Claude model name.
    """
    role = config.resolve_role(role_name, runtime_override=runtime_override)
    runtime = get_runtime(role.runtime)
    if not runtime.available():
        raise RuntimeError(f"Runtime {runtime.name!r} CLI is not installed/on PATH")
    request = RunRequest(
        prompt=prompt,
        cwd=cwd,
        model=role.model,
        max_turns=role.max_turns,
        permission_mode=role.permission_mode,
        stream=config.runtime.stream,
        fallback_models=tuple(role.fallbacks),
        # every role may run the gates: implementer to iterate, planner to
        # reproduce, reviewer to verify — write access still differs by mode
        allowed_tools=gate_allowed_tools(config) + extra_allowed_tools,
        idle_timeout_seconds=config.loop.idle_timeout_seconds,
        run_timeout_seconds=config.loop.run_timeout_seconds,
    )
    return runtime, request


def make_plan(
    config: ProjectConfig,
    issue: dict[str, Any],
    cwd: Path,
    *,
    runtime_override: str | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> tuple[RunRequest, RunResult]:
    """Run the planner role.

    Returns the request and its result so callers can attribute the plan to the
    model that actually wrote it; raises on ESCALATE or failure.
    """
    prompt = render_task(
        "plan",
        issue_number=str(issue["number"]),
        issue_title=issue["title"],
        issue_body=issue.get("body") or "(no description)",
        issue_labels=_labels(issue),
        issue_comments=_format_comments(issue),
    )
    runtime, request = role_request(
        config, "planner", prompt, cwd, runtime_override=runtime_override
    )
    result = run_with_fallback(runtime, request, on_event=log)
    if not result.ok:
        raise RuntimeError(f"Planner run failed: {result.text}")
    if escalated(result.text):
        raise RuntimeError(f"Planner escalated:\n{result.text}")
    if opens_with_escalation_word(result.text):
        log(
            "note: the plan opens with the word ESCALATE but not as the sentinel "
            "(`ESCALATE:` or a line of its own) — treating it as a plan"
        )
    return request, result


def plan_command(
    project_root: Path,
    issue_number: int,
    *,
    post_comment: bool = False,
    runtime_name: str | None = None,
) -> list[str]:
    """Argv that re-runs this plan inline, for spawning onto a surface."""
    command = ["agent", "plan", str(issue_number)]
    if post_comment:
        command.append("--post")
    if runtime_name:
        command += ["--runtime", runtime_name]
    return command + ["--project", str(project_root)]


def dispatch_plan(
    project_root: Path,
    issue_number: int,
    *,
    surface_name: str = "auto",
    post_comment: bool = False,
    runtime_name: str | None = None,
) -> surfaces.Spawned:
    """Spawn `agent plan` on a visible surface; return where it went.

    Like a review, a plan is read-only and has no task worktree, so it attaches
    to the project's own card — no `attach_path`. No spawn record either: a
    plan is not a run `agent runs` tracks, so nothing ever waits on one.
    """
    chosen = surfaces.pick(surface_name)
    command = plan_command(
        project_root, issue_number, post_comment=post_comment, runtime_name=runtime_name
    )
    return chosen.spawn(f"agent-plan-issue-{issue_number}", command, project_root)


class _CardReporter:
    """Wraps `orca.report` for one run: same fallback, one warning if it dies.

    `orca.report`'s fallback to the project-root card covers the common case
    (see `surfaces.OrcaSurface`, issue #20/#68); if both the worktree and the
    root card fail, every subsequent `.note()` in this run would silently
    drop status too — warn exactly once instead of staying quiet (issue #68).
    """

    def __init__(self, project_root: Path, wt_path: Path, log: Callable[[str], None]) -> None:
        self._project_root = project_root
        self._wt_path = wt_path
        self._card = wt_path
        self._log = log
        self._warned = False

    def note(self, comment: str, *, status: str | None = None) -> None:
        ok = orca.report(
            self._card, comment=comment, status=status, fallback_path=self._project_root
        )
        # Stick to the fallback once used. The surface gives up on the worktree
        # card after ~4s and pins the terminal to the root card, but indexing
        # lands at 13-25s — so without this, later notes would drift back to a
        # worktree card that has no terminal on it, leaving the human watching
        # a stale "planning" and never seeing "PR opened".
        if ok == self._project_root:
            self._card = self._project_root
        if ok or self._warned or not orca.available():
            return
        self._warned = True
        self._log(
            f"Orca card {self._wt_path} is not indexed and neither is the project root card — "
            "status updates for this run won't appear on any card"
        )


def run_implement(
    project_root: Path,
    issue_number: int,
    *,
    runtime_name: str | None = None,
    open_pr: bool = True,
    keep_worktree: bool = False,
    plan_file: Path | None = None,
    grant_file: Path | None = None,
    force: bool = False,
    log: Callable[[str], None] = flush_print,
) -> bool:
    """Issue → worktree → plan (smart model) → implement loop → self-review → PR.

    Each stage runs as a separate agent with fresh context: the planner and
    reviewer roles default to a stronger model in read-only mode, the
    implementer does the bulk work (see `agents:` in config). Returns True on
    success. On implement/review failure the worktree is kept for inspection.

    The issue is claimed for the duration (issue #131) so the scheduled CI lane
    does not start a second run on it. The release is here, in a `finally`,
    rather than at each of the terminal paths inside: there is no exit from this
    function with an agent still working, so one unconditional release cannot
    drift out of step with the eight ways a run can end — including an
    exception, which none of those paths would have covered.
    """
    claim = claims.Claim(project_root, issue_number, log=log)
    try:
        return _run_implement(
            project_root,
            issue_number,
            claim=claim,
            runtime_name=runtime_name,
            open_pr=open_pr,
            keep_worktree=keep_worktree,
            plan_file=plan_file,
            grant_file=grant_file,
            force=force,
            log=log,
        )
    finally:
        claim.release()


def _run_implement(
    project_root: Path,
    issue_number: int,
    *,
    claim: claims.Claim,
    runtime_name: str | None = None,
    open_pr: bool = True,
    keep_worktree: bool = False,
    plan_file: Path | None = None,
    grant_file: Path | None = None,
    force: bool = False,
    log: Callable[[str], None] = flush_print,
) -> bool:
    """`run_implement`'s body, minus the claim bookkeeping that wraps it."""
    config = load_project_config(project_root)
    issue = github.get_issue(issue_number, cwd=project_root)
    task_id, branch = task_identifiers(issue_number)

    if not force:
        existing = github.open_prs_for_issue(issue_number, cwd=project_root)
        if existing:
            pr = existing[0]
            log(
                f"issue #{issue_number} already has open PR #{pr['number']} ({pr['url']}) — "
                "review/merge that instead, or close it to re-dispatch. Pass --force to override."
            )
            return False

    log(f"creating worktree for {branch} from {config.base_branch}")
    # reuse: `agent dispatch` pre-creates this worktree so the surface can
    # attach to it; a pristine checkout on our branch is ours to take over.
    wt_path = worktree.create(
        project_root, config.worktree_dir, task_id, branch, config.base_branch, reuse=True
    )
    # A new cycle owns this issue's status from here on. Deliberately after the
    # already-has-an-open-PR bail-out above, which starts nothing and so has no
    # business discarding the previous cycle's record — or taking a claim.
    runs.clear_outcome(project_root, issue_number, log=log)
    claim.take()
    card = _CardReporter(project_root, wt_path, log)
    card.note(f"#{issue_number}: setting up", status=orca.STATUS_IN_PROGRESS)

    if config.commands.setup:
        log(f"setup: {config.commands.setup}")
        try:
            # Project-configured shell like a gate (`npm install` and friends),
            # so it shares the gate bound rather than `run`'s short default.
            run(
                ["sh", "-c", config.commands.setup],
                cwd=wt_path,
                timeout=config.loop.gate_timeout_seconds,
            )
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
        planner_role = config.resolve_role("planner", runtime_override=runtime_name)
        log(f"planning (model: {planner_role.model or 'default'})")
        card.note(f"#{issue_number}: planning")
        try:
            plan_request, plan_result = make_plan(
                config, issue, wt_path, runtime_override=runtime_name, log=log
            )
        except RuntimeError as exc:
            log(str(exc))
            _abort_cleanly(project_root, config, task_id, log)
            log("issue needs a human decision")
            return False
        plan = plan_result.text
        log(f"plan ready ({len(plan.splitlines())} lines, {model_note(plan_request, plan_result)})")

    grant = _resolve_grant(project_root, issue_number, grant_file, log=log)

    prompt = render_task(
        "implement",
        issue_number=str(issue["number"]),
        issue_title=issue["title"],
        issue_body=issue.get("body") or "(no description)",
        issue_labels=_labels(issue),
        branch=branch,
        plan=plan,
        skills=load_skills(config.skills, project_root),
        authorization=_render_authorization(grant),
    )
    runtime, request = role_request(
        config, "implementer", prompt, wt_path, runtime_override=runtime_name
    )

    card.note(f"#{issue_number}: implementing")
    outcome = run_task_loop(runtime, request, config, wt_path, on_event=log)
    if not outcome.ok:
        failing = ", ".join(g.name for g in outcome.gate_failures)
        log(
            f"FAILED after {outcome.attempts} attempts; worktree kept at {wt_path} "
            f"for inspection. Failing gates: {failing}"
        )
        card.note(f"#{issue_number}: FAILED gates ({failing}); worktree kept")
        # From the outside this exit is indistinguishable from an abandoned
        # run — worktree kept, no PR, no feedback — so a supervisor can only
        # reach `stopped` for it, and only after a two-poll debounce. Saying
        # so directly is the whole point of the channel (issue #98).
        messages.send_outcome(
            project_root,
            issue_number,
            state="failed",
            reason=f"gates failed ({failing}) after {outcome.attempts} attempts; "
            f"worktree kept at {wt_path}",
            log=log,
        )
        return False

    implementer_text = outcome.last_result.text if outcome.last_result else ""
    if not _review_and_maybe_halt(
        config,
        project_root,
        issue_number,
        wt_path,
        implementer_text,
        card=card,
        runtime_name=runtime_name,
        grant=grant,
        log=log,
    ):
        return False

    return _finish_run(
        project_root,
        config,
        issue,
        issue_number,
        task_id,
        branch,
        wt_path,
        request,
        runtime,
        outcome,
        card=card,
        open_pr=open_pr,
        keep_worktree=keep_worktree,
        grant=grant,
        log=log,
    )


def _finish_run(
    project_root: Path,
    config: ProjectConfig,
    issue: dict[str, Any],
    issue_number: int,
    task_id: str,
    branch: str,
    wt_path: Path,
    request: RunRequest,
    runtime: Runtime,
    outcome: LoopOutcome,
    *,
    card: _CardReporter,
    open_pr: bool,
    keep_worktree: bool,
    grant: grants.Grant | None = None,
    log: Callable[[str], None],
) -> bool:
    """Commit, push, open the PR (maybe auto-merge), and clean up the worktree.

    Shared tail for `run_implement` and `run_resume`: once the loop and
    self-review have passed, landing the change is identical either way.
    """
    diff_stat = run(["git", "diff", "--stat"], cwd=wt_path).stdout.strip()
    log(f"changes:\n{diff_stat}")

    title = f"fix: {issue['title']} (#{issue_number})"
    run(["git", "add", "-A"], cwd=wt_path)
    worktree.commit(wt_path, title)

    pr_url: str | None = None
    if open_pr:
        run(["git", "push", "-u", "origin", branch], cwd=wt_path, timeout=SLOW_GIT_TIMEOUT_S)
        used_model = (outcome.last_result.model if outcome.last_result else None) or request.model
        body = (
            f"Closes #{issue_number}.\n\n"
            f"Automated implementation via agent-ops "
            f"({runtime.name}, model {used_model or 'runtime default'}, "
            f"{outcome.attempts} attempt(s), gates passed)."
        )
        if used_model != request.model:
            body += (
                f"\n\n> **Model fallback:** the configured model `{request.model}` was "
                f"unavailable, so this was implemented by `{used_model}` instead."
            )
        if grant is not None:
            body += _authorization_pr_section(grant)
        url = github.create_pr(wt_path, base=config.base_branch, title=title, body=body)
        pr_url = url
        log(f"opened PR: {url}")
        card.note(f"#{issue_number}: PR opened {url}", status=orca.STATUS_IN_REVIEW)
        if config.loop.auto_merge:
            from agent_ops.workflows.merge import run_merge

            pr_number = int(url.rstrip("/").rsplit("/", 1)[-1])
            log("auto-merge enabled — applying merge rules")
            # never overrides: a blocked PR stays open for a human
            run_merge(project_root, pr_number, log=log)

    # Durable record of success, written before the signals below are cleared:
    # `discover_runs` falls back to this once the worktree/feedback/open-PR
    # signals it normally reads are gone (issue #87). Order matters — if the
    # process is interrupted between this write and the feedback-file unlink,
    # both files briefly coexist, and the outcome record takes precedence over
    # a stale feedback file in `runs.classify`, so that's safe.
    runs.write_outcome(project_root, issue_number, state="done", pr_url=pr_url, log=log)

    # Both callers, not just resume: a successful implement leaves any earlier
    # cycle's findings behind too, and a later `agent resume` on this issue
    # would silently hand the agent a review it has already addressed. Before
    # the worktree removal, so a failure there can't strand them.
    _feedback_path(project_root, issue_number).unlink(missing_ok=True)
    _ad_hoc_message_path(project_root, issue_number).unlink(missing_ok=True)
    grants.persist_path(project_root, issue_number).unlink(missing_ok=True)
    if not keep_worktree:
        worktree.remove(project_root, config.worktree_dir, task_id, force=True)
        log("worktree removed (branch kept)")
    # The same facts as the durable record above, pushed to whoever is waiting
    # rather than left to be discovered (issue #98). Last thing this run does,
    # deliberately: a supervisor treats a pushed outcome as terminal on the
    # spot, so announcing `done` any earlier would release it while the
    # cleanup above was still running.
    messages.send_outcome(project_root, issue_number, state="done", pr_url=pr_url, log=log)
    return True


def _resolve_feedback(
    project_root: Path,
    issue_number: int,
    *,
    message: str | None,
    message_file: Path | None,
) -> str:
    """Feedback text, in priority order: `--message`, `--message-file`, the stored halt file."""
    if message is not None and message_file is not None:
        raise ValueError("pass either --message or --message-file, not both")
    if message is not None:
        return message
    if message_file is not None:
        return message_file.read_text()
    halt_path = _feedback_path(project_root, issue_number)
    if not halt_path.is_file():
        raise FileNotFoundError(
            f"no feedback for issue #{issue_number} — pass --message or --message-file, "
            f"or check {halt_path}"
        )
    return halt_path.read_text()


def run_resume(
    project_root: Path,
    issue_number: int,
    *,
    message: str | None = None,
    message_file: Path | None = None,
    runtime_name: str | None = None,
    open_pr: bool = True,
    keep_worktree: bool = False,
    grant_file: Path | None = None,
    log: Callable[[str], None] = flush_print,
) -> bool:
    """Resume the implementer role in a worktree a prior run halted on (e.g. self-review).

    Feedback comes from `message`, `message_file`, or — the common case, right
    after a halt — the file `_record_halt` wrote. Runs the same loop →
    self-review → PR tail as `run_implement`, on the existing worktree instead
    of a fresh one.

    Claimed and released exactly as `run_implement` is, and for the same reason:
    a resume is a run, and the halt that preceded it released the issue.
    """
    claim = claims.Claim(project_root, issue_number, log=log)
    try:
        return _run_resume(
            project_root,
            issue_number,
            claim=claim,
            message=message,
            message_file=message_file,
            runtime_name=runtime_name,
            open_pr=open_pr,
            keep_worktree=keep_worktree,
            grant_file=grant_file,
            log=log,
        )
    finally:
        claim.release()


def _run_resume(
    project_root: Path,
    issue_number: int,
    *,
    claim: claims.Claim,
    message: str | None = None,
    message_file: Path | None = None,
    runtime_name: str | None = None,
    open_pr: bool = True,
    keep_worktree: bool = False,
    grant_file: Path | None = None,
    log: Callable[[str], None] = flush_print,
) -> bool:
    """`run_resume`'s body, minus the claim bookkeeping that wraps it."""
    config = load_project_config(project_root)
    task_id, branch = task_identifiers(issue_number)
    # Resolved before recovering the worktree: a missing-feedback error must
    # not perform worktree recovery (fetch, recreate, possible fast-forward)
    # as a side effect of a call that is about to fail anyway.
    feedback = _resolve_feedback(
        project_root, issue_number, message=message, message_file=message_file
    )
    wt_path = _existing_worktree(project_root, config, issue_number, log=log)
    card = _CardReporter(project_root, wt_path, log)
    if not _refuse_if_stale_base(config, project_root, issue_number, wt_path, card=card, log=log):
        return False

    issue = github.get_issue(issue_number, cwd=project_root)
    diff_stat = run(["git", "diff", "--stat"], cwd=wt_path).stdout.strip() or "(no changes yet)"
    grant = _resolve_grant(project_root, issue_number, grant_file, log=log)

    prompt = render_task(
        "resume",
        issue_number=str(issue["number"]),
        issue_title=issue["title"],
        issue_body=issue.get("body") or "(no description)",
        issue_labels=_labels(issue),
        branch=branch,
        diff_stat=diff_stat,
        feedback=feedback,
        skills=load_skills(config.skills, project_root),
        authorization=_render_authorization(grant),
    )
    runtime, request = role_request(
        config, "implementer", prompt, wt_path, runtime_override=runtime_name
    )

    # Same as `run_implement`: whatever a previous cycle recorded, this one is
    # now the current word on the issue. Placed after `_resolve_feedback`,
    # which raises when there is nothing to resume from — no cycle starts then,
    # so there is nothing to claim either.
    runs.clear_outcome(project_root, issue_number, log=log)
    claim.take()
    card.note(f"#{issue_number}: resuming")
    outcome = run_task_loop(runtime, request, config, wt_path, on_event=log)
    if not outcome.ok:
        failing = ", ".join(g.name for g in outcome.gate_failures)
        log(
            f"FAILED after {outcome.attempts} attempts; worktree kept at {wt_path} "
            f"for inspection. Failing gates: {failing}"
        )
        card.note(f"#{issue_number}: FAILED gates ({failing}); worktree kept")
        # From the outside this exit is indistinguishable from an abandoned
        # run — worktree kept, no PR, no feedback — so a supervisor can only
        # reach `stopped` for it, and only after a two-poll debounce. Saying
        # so directly is the whole point of the channel (issue #98).
        messages.send_outcome(
            project_root,
            issue_number,
            state="failed",
            reason=f"gates failed ({failing}) after {outcome.attempts} attempts; "
            f"worktree kept at {wt_path}",
            log=log,
        )
        return False

    implementer_text = outcome.last_result.text if outcome.last_result else ""
    if not _review_and_maybe_halt(
        config,
        project_root,
        issue_number,
        wt_path,
        implementer_text,
        card=card,
        runtime_name=runtime_name,
        grant=grant,
        log=log,
    ):
        return False

    ok = _finish_run(
        project_root,
        config,
        issue,
        issue_number,
        task_id,
        branch,
        wt_path,
        request,
        runtime,
        outcome,
        card=card,
        open_pr=open_pr,
        keep_worktree=keep_worktree,
        grant=grant,
        log=log,
    )
    return ok


def resume_command(
    project_root: Path,
    issue_number: int,
    *,
    message_file: Path,
    runtime_name: str | None = None,
    open_pr: bool = True,
    keep_worktree: bool = False,
    grant_file: Path | None = None,
) -> list[str]:
    """Argv that re-runs this resume inline, for spawning onto a surface.

    Feedback always travels as a file path here, never `--message`: the
    surface's argv is what a hand-rolled terminal command got wrong (#73),
    and a path is immune to shell quoting the way inline text is not.
    """
    command = [
        "agent",
        "resume",
        str(issue_number),
        "--surface",
        "inline",
        "--message-file",
        str(message_file),
    ]
    if runtime_name:
        command += ["--runtime", runtime_name]
    if not open_pr:
        command.append("--no-pr")
    if keep_worktree:
        command.append("--keep-worktree")
    if grant_file:
        # Absolute, same as `--plan-file` in `cli.dispatch`: the surface may
        # spawn from the worktree rather than the caller's cwd.
        command += ["--grant-file", str(grant_file)]
    return command + ["--project", str(project_root)]


def dispatch_resume(
    project_root: Path,
    issue_number: int,
    *,
    surface_name: str = "auto",
    message: str | None = None,
    message_file: Path | None = None,
    runtime_name: str | None = None,
    open_pr: bool = True,
    keep_worktree: bool = False,
    grant_file: Path | None = None,
    log: Callable[[str], None] = flush_print,
) -> surfaces.Spawned:
    """Resolve the existing task worktree and spawn `agent resume` attached to it.

    The worktree is resolved before anything else: a missing one must fail
    fast with no surface spawned, not leave a dangling terminal that dies
    seconds later the way the hand-rolled `orca terminal create` attempts did.
    """
    config = load_project_config(project_root)
    wt_path = _existing_worktree(project_root, config, issue_number, log=log)

    if message is not None and message_file is not None:
        raise ValueError("pass either --message or --message-file, not both")
    if message is not None:
        feedback_path = _ad_hoc_message_path(project_root, issue_number)
        feedback_path.parent.mkdir(exist_ok=True)
        feedback_path.write_text(message)
    elif message_file is not None:
        feedback_path = message_file.resolve()
        if not feedback_path.is_file():
            raise CommandError(f"message file not found: {feedback_path}")
    else:
        feedback_path = _feedback_path(project_root, issue_number)
        if not feedback_path.is_file():
            raise FileNotFoundError(
                f"no feedback for issue #{issue_number} — pass --message or --message-file, "
                f"or check {feedback_path}"
            )

    if grant_file is not None:
        grant_file = grant_file.resolve()
        if not grant_file.is_file():
            raise CommandError(f"grant file not found: {grant_file}")

    chosen = surfaces.pick(surface_name)
    command = resume_command(
        project_root,
        issue_number,
        message_file=feedback_path,
        runtime_name=runtime_name,
        open_pr=open_pr,
        keep_worktree=keep_worktree,
        grant_file=grant_file,
    )
    spawned = chosen.spawn(
        f"agent-resume-issue-{issue_number}", command, project_root, attach_path=wt_path
    )
    # Overwrites whatever the original dispatch recorded: this terminal is the
    # one that owns the issue now, so it is the mailbox a supervisor should
    # watch (issue #98). The spawner carries forward rather than resetting:
    # `current_handle()` is whoever ran `agent resume` (the supervisor, in the
    # dispatch-then-resume pattern this repo uses), falling back to the prior
    # record's spawner when resumed from a plain shell — dropping it silences
    # every halt from round 2 onward (issue #192).
    prior = messages.load_spawn(project_root, issue_number, log=log)
    spawner = messages.current_handle() or (prior.spawner if prior is not None else None)
    if spawner == spawned.handle:
        spawner = None  # never record the worker as its own spawner
    messages.record_spawn(
        project_root,
        issue_number,
        surface=spawned.surface,
        handle=spawned.handle,
        pid=spawned.pid,
        log_path=spawned.log_path,
        spawner=spawner,
        log=log,
    )
    return spawned


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


_MAX_COMMENTS = 20
_PINNED_PREFIXES = ("## Agent spec", "## Agent plan")


def _is_pinned(comment: dict[str, Any]) -> bool:
    """A `## Agent spec` / `## Agent plan` comment must never be dropped by the cap."""
    body = (comment.get("body") or "").lstrip()
    return body.startswith(_PINNED_PREFIXES)


def _render_comment(comment: dict[str, Any]) -> str:
    author = (comment.get("author") or {}).get("login") or "unknown"
    created_at = comment.get("createdAt", "")
    body = comment.get("body", "")
    return f"**{author}** ({created_at}):\n{body}"


def _format_comments(issue: dict[str, Any]) -> str:
    """Render issue comments for the planner prompt.

    The CI-lane planner (prompts/agents/planner.md) gets the full issue thread
    so it can build on an approved `## Agent spec` / `## Agent plan` comment;
    this mirrors that for the local lane. Capped to the most recent
    `_MAX_COMMENTS` (tail, not head) so a long thread can't blow up the prompt —
    but any `## Agent spec` / `## Agent plan` comment outside that tail is
    pinned and included anyway, since the whole point of this issue is that
    the planner must see an approved spec/plan even on long-lived threads
    where it's since scrolled out of the recent window.
    """
    comments = issue.get("comments") or []
    if not comments:
        return "(no comments)"

    recent = comments[-_MAX_COMMENTS:]
    older = comments[:-_MAX_COMMENTS]  # empty when len(comments) <= _MAX_COMMENTS
    pinned_older = [c for c in older if _is_pinned(c)]

    if not pinned_older:
        return "\n\n---\n\n".join(_render_comment(c) for c in recent)

    pinned_section = "\n\n---\n\n".join(_render_comment(c) for c in pinned_older)
    recent_section = "\n\n---\n\n".join(_render_comment(c) for c in recent)
    return (
        "### Pinned spec/plan comments (older than the recent window below)\n\n"
        f"{pinned_section}\n\n"
        "### Recent comments\n\n"
        f"{recent_section}"
    )


@dataclass(frozen=True)
class SelfReview:
    ok: bool
    text: str
    reviewed: bool = True
    """False when there was nothing to review, which is not a rejection.

    A truly empty diff — the implementer changed nothing at all. (Untracked
    files are covered: `_self_review` stages intent-to-add first, so a
    create-only run is reviewed, not skipped.) Treating this as REQUEST
    CHANGES would post "changes requested — (empty diff)" on the issue and
    store it as resume feedback, telling the next run to address a message
    that says nothing.
    """


def _self_review(
    config: ProjectConfig,
    wt_path: Path,
    *,
    log: Callable[[str], None],
    runtime_override: str | None = None,
) -> SelfReview:
    # Intent-to-add first: `git diff` ignores untracked files, so an
    # implementer whose change is only *new* files — the common shape for "add
    # X" issues — would otherwise produce an empty diff and go unreviewed, or
    # in the mixed case get approved on the one modified file the reviewer
    # could see. Harmless for the later `git add -A` + commit.
    run(["git", "add", "-A", "-N"], cwd=wt_path, check=False)
    diff = run(["git", "diff"], cwd=wt_path).stdout
    if not diff.strip():
        log("self-review skipped: empty diff")
        return SelfReview(False, "(empty diff — nothing to review)", reviewed=False)
    prompt = render_task("review", diff=diff, context="Pre-commit self-review of local changes.")
    runtime, request = role_request(
        config, "reviewer", prompt, wt_path, runtime_override=runtime_override
    )
    result = run_with_fallback(runtime, request, on_event=log)
    # `verdict_of` returning "unknown" (no `VERDICT:` line) makes this False —
    # an unparseable self-review halts the run rather than proceeding (fail-closed).
    verdict_ok = result.ok and verdict_of(result.text) == "approve"
    log(
        f"self-review verdict: {'APPROVE' if verdict_ok else 'REQUEST CHANGES'} "
        f"({model_note(request, result)})"
    )
    if not verdict_ok:
        log(result.text)
    return SelfReview(verdict_ok, result.text)


def _drift_message(drift: worktree.BaseDrift, wt_path: Path, issue_number: int) -> str:
    fetch_note = "" if drift.fetched else ", fetch failed — counted against last-known remote"
    return (
        f"base is {drift.behind} commit(s) behind {drift.base_ref} "
        f"(tip: {drift.tip}{fetch_note}); rebase in {wt_path} "
        f"(uncommitted work preserved), then agent resume {issue_number}"
    )


def _refuse_if_stale_base(
    config: ProjectConfig,
    project_root: Path,
    issue_number: int,
    wt_path: Path,
    *,
    card: _CardReporter,
    log: Callable[[str], None],
) -> bool:
    """Refuse to proceed (self-review or PR) against a base that has moved on.

    A stale tree reads missing merged work as a missing feature, or already-
    merged work as new — both produce a confident, wrong review (issue #184).
    `checked=False` (no remote base ref, or the worktree can't be inspected)
    is not "current" — it is "unknown", so it is allowed through rather than
    blocked; the run's own gates are the fallback. Never rebases or resets:
    that decision, and any conflict it produces, is left to a human.
    """
    drift = worktree.base_drift(wt_path, config.base_branch)
    if not drift.checked:
        log(f"base drift check skipped: no {drift.base_ref} to compare against")
        return True
    if drift.behind == 0:
        return True
    message = _drift_message(drift, wt_path, issue_number)
    log(f"refusing: {message}")
    card.note(f"#{issue_number}: {message}")
    messages.send_outcome(
        project_root,
        issue_number,
        state="failed",
        reason=message,
        log=log,
    )
    return False


def _check_grant_scope(
    config: ProjectConfig,
    project_root: Path,
    issue_number: int,
    wt_path: Path,
    grant: grants.Grant | None,
    *,
    card: _CardReporter,
    log: Callable[[str], None],
) -> bool:
    """Refuse when a granted run's changes reach outside what the grant covers (issue #200).

    A no-op when there is no grant: a grantless run is governed entirely by
    the implementer prompt's standing danger-zone refusal, unchanged by this
    check — this only ever narrows what an *authorized* run may touch, it
    never adds a new restriction to the default path.
    """
    if grant is None:
        return True
    changed = worktree.changed_paths(wt_path)
    violations = grants.scope_violations(changed, grant, config.merge.blocked_paths)
    if not violations:
        return True
    offenders = ", ".join(violations)
    message = (
        f"changes reach outside the granted scope ({grant.scope!r}): {offenders} — "
        f"worktree kept at {wt_path}"
    )
    log(f"refusing: {message}")
    card.note(f"#{issue_number}: {message}")
    runs.write_outcome(project_root, issue_number, state="failed", reason=message, log=log)
    messages.send_outcome(project_root, issue_number, state="failed", reason=message, log=log)
    return False


def _truncated_first_line(text: str, limit: int = 200) -> str:
    """The first non-empty line of `text`, trimmed to `limit` characters.

    Feeds the `failed`/`halted` outcome's `reason`: the full transcript
    already lives in the run log for dispatched runs, so the reason only
    needs to be a pointer, not a copy.
    """
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if not first:
        return "(empty message)"
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def _clear_stale_feedback(
    project_root: Path, issue_number: int, *, log: Callable[[str], None]
) -> None:
    """Drop any earlier cycle's findings when this run declined to act.

    A decline supersedes what came before it: this run looked at the
    situation and had nothing to add, so a later bare `agent resume` must not
    replay context from before the decline (issue #209). Matters most on the
    escalation path, which deliberately writes no feedback of its own — a
    file left over from an earlier halt would otherwise be resumed as if it
    were this run's verdict.

    Best-effort, matching the module's convention (`_record_halt`,
    `runs.clear_outcome`): a file that refuses to delete must not turn a
    decline into a crash.
    """
    feedback_path = _feedback_path(project_root, issue_number)
    try:
        feedback_path.unlink(missing_ok=True)
    except OSError as exc:
        log(f"could not clear stale feedback file at {feedback_path}: {exc}")


def _handle_empty_diff(
    project_root: Path,
    issue_number: int,
    wt_path: Path,
    implementer_text: str,
    *,
    card: _CardReporter,
    log: Callable[[str], None],
) -> bool:
    """True when the diff is empty and a durable outcome has been recorded for it.

    False means either there is a diff to act on, or the diff itself could
    not be determined (e.g. `wt_path` doesn't exist, as in some tests) — a
    caller that gets False should proceed to whatever check normally follows,
    exactly as it did before this existed.

    Detection reuses `_self_review`'s exact recipe (intent-to-add, then `git
    diff`) so a create-only run counts as "not empty" here too (#75). Called
    from `_review_and_maybe_halt` before the `config.loop.self_review` early
    return, so an empty diff is caught even with self-review disabled —
    otherwise `_finish_run` would `git commit` on a clean tree and raise
    (issue #199).

    An implementer that produces no changes is not a pass, and it is not
    folded into the "gates passed on an unchanged tree" success signal. It is
    one of two distinct, named outcomes instead:

    - `escalated(implementer_text)` — a deliberate refusal. Recorded
      `halted`, with the full final message posted on the issue (mirrors the
      CI lane's "escalation posts its reasoning on the issue", #129).
      Deliberately does NOT write the feedback file: a bare `agent resume`
      must not replay a refusal as actionable feedback — a human resumes
      with `--message` once they've decided what to do about it.
    - Otherwise — an unproductive attempt. Recorded `failed`, with an excerpt
      of the final message as the reason.
    """
    run(["git", "add", "-A", "-N"], cwd=wt_path, check=False)
    diff = run(["git", "diff"], cwd=wt_path, check=False)
    if diff.returncode != 0 or diff.stdout.strip():
        return False

    if escalated(implementer_text):
        log(f"implementer escalated with an empty diff; worktree kept at {wt_path}")
        card.note(f"#{issue_number}: implementer escalated — needs a human")
        try:
            github.comment_on_issue(
                issue_number,
                f"## Agent implement — escalated\n\n{implementer_text}",
                cwd=project_root,
            )
        except CommandError as exc:
            # Same best-effort guard as `_record_halt`: a comment that can't
            # be posted (missing `gh`, no remote) must not turn a halt into a
            # crash — the durable record below is written either way.
            log(f"could not post escalation comment on issue #{issue_number}: {exc}")
        reason = (
            f"implementer escalated: {_truncated_first_line(implementer_text)} — "
            f"worktree kept at {wt_path}"
        )
        # Durable record first, then the best-effort push — mirrors
        # `spawn.report`'s ordering.
        _clear_stale_feedback(project_root, issue_number, log=log)
        runs.write_outcome(project_root, issue_number, state="halted", reason=reason, log=log)
        messages.send_outcome(project_root, issue_number, state="halted", reason=reason, log=log)
        return True

    if opens_with_escalation_word(implementer_text):
        log(
            "note: the implementer's final message opens with the word ESCALATE but not "
            "as the sentinel (`ESCALATE:` or a line of its own) — treating this as an "
            "unproductive attempt, not an escalation"
        )
    log(f"implementer finished with an empty diff and no ESCALATE; worktree kept at {wt_path}")
    card.note(f"#{issue_number}: empty diff, no escalation; worktree kept")
    reason = (
        "implementer finished with an empty diff and no ESCALATE — final message: "
        f"{_truncated_first_line(implementer_text)} — worktree kept at {wt_path}"
    )
    _clear_stale_feedback(project_root, issue_number, log=log)
    runs.write_outcome(project_root, issue_number, state="failed", reason=reason, log=log)
    messages.send_outcome(project_root, issue_number, state="failed", reason=reason, log=log)
    return True


def _review_and_maybe_halt(
    config: ProjectConfig,
    project_root: Path,
    issue_number: int,
    wt_path: Path,
    implementer_text: str,
    *,
    card: _CardReporter,
    runtime_name: str | None,
    grant: grants.Grant | None = None,
    log: Callable[[str], None],
) -> bool:
    """Run self-review if enabled; on REQUEST CHANGES, record the halt. True means proceed.

    Reports through the run's `_CardReporter` rather than `orca.report`
    directly, so a halt lands on the same card the rest of the run did — the
    fallback is sticky, and the worktree card may not be the one with the
    terminal on it (#68).
    """
    if not _refuse_if_stale_base(config, project_root, issue_number, wt_path, card=card, log=log):
        return False
    if _handle_empty_diff(
        project_root, issue_number, wt_path, implementer_text, card=card, log=log
    ):
        return False
    if not _check_grant_scope(
        config, project_root, issue_number, wt_path, grant, card=card, log=log
    ):
        return False
    if not config.loop.self_review:
        return True
    review = _self_review(config, wt_path, log=log, runtime_override=runtime_name)
    if review.ok:
        return True
    if not review.reviewed:
        # Defensive delegate, not the primary path: `_handle_empty_diff` above
        # already covers every empty-diff case with the same git-diff recipe,
        # so this should be unreachable in practice. Kept rather than removed
        # so a future divergence between the two checks still writes a
        # durable outcome record instead of falling back to the silent
        # "stopped — inspect" shape issue #199 is about.
        if _handle_empty_diff(
            project_root, issue_number, wt_path, implementer_text, card=card, log=log
        ):
            return False
        log(f"self-review had nothing to review; worktree kept at {wt_path}")
        card.note(f"#{issue_number}: self-review had nothing to review")
        _clear_stale_feedback(project_root, issue_number, log=log)
        reason = f"self-review had nothing to review; worktree kept at {wt_path}"
        runs.write_outcome(project_root, issue_number, state="failed", reason=reason, log=log)
        messages.send_outcome(project_root, issue_number, state="failed", reason=reason, log=log)
        return False
    log(f"self-review requested changes; worktree kept at {wt_path}")
    card.note(f"#{issue_number}: self-review requested changes")
    _record_halt(project_root, issue_number, review.text, log=log)
    return False


def _record_halt(
    project_root: Path,
    issue_number: int,
    feedback: str,
    *,
    log: Callable[[str], None],
) -> None:
    """Stash the review findings for `agent resume` and mark the issue as halted, not unstarted.

    Every step here is best-effort: a missing `gh` remote (test or scratch
    repos), an outcome record that won't delete, or a message that cannot be
    pushed, must not turn a halt into a crash.
    """
    # Before the feedback file, not after: `runs.classify` ranks `outcome`
    # above `has_feedback`, so a record left over from an earlier cycle would
    # shadow this halt and report `done` — the user would never be told to
    # `agent resume`. `run_implement`/`run_resume` already clear it at the top
    # of a cycle; this repeats it because a halt is the one exit where being
    # shadowed is actively harmful, and the record is cheap to remove twice.
    runs.clear_outcome(project_root, issue_number, log=log)
    # Sent as an escalation rather than a plain completion: this run stopped
    # early and cannot continue without a human, which is exactly the state a
    # supervisor cannot tell apart from an ordinary pause by watching the
    # terminal (issue #98). The findings themselves stay in the file below —
    # the message carries the verdict and where to look, not the prose.
    messages.send_outcome(
        project_root,
        issue_number,
        state="halted",
        reason=f"self-review requested changes — resume with `agent resume {issue_number}`",
        log=log,
    )
    path = _feedback_path(project_root, issue_number)
    try:
        path.parent.mkdir(exist_ok=True)
        path.write_text(feedback)
    except OSError as exc:
        # Outside the try this would crash the halt before the comment that
        # makes it visible — the opposite of best-effort.
        log(f"could not stash halt feedback at {path}: {exc}")
    try:
        github.comment_on_issue(
            issue_number,
            "## Agent self-review — changes requested\n\n"
            f"{feedback}\n\n"
            f"Resume with `agent resume {issue_number}`.",
            cwd=project_root,
        )
    except CommandError as exc:
        # `gh` missing from PATH raises this same `CommandError` (utils.run
        # converts it under `check=True`, agent-ops#154), and this must never
        # turn a halt into a crash, as the docstring promises.
        log(f"could not post halt comment on issue #{issue_number}: {exc}")
