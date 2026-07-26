from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

import typer

from agent_ops import __version__, github, messages, registry, stubs, surfaces, worktree
from agent_ops.config import (
    PROJECT_CONFIG_REL,
    ProjectConfig,
    ladder_warnings,
    load_project_config,
    role_reports,
    runtime_reports,
)
from agent_ops.fallback import artifact_footer
from agent_ops.runtimes import get_runtime, runtime_names
from agent_ops.utils import PLATFORM_ROOT, CommandError, run
from agent_ops.workflows import (
    dispatch_plan,
    dispatch_resume,
    dispatch_review,
    format_summary,
    report_outcome,
    run_implement,
    run_resume,
    run_review,
    run_reviews,
    run_spawn,
)
from agent_ops.workflows.implement import make_plan, task_identifiers
from agent_ops.workflows.merge import run_merge, run_promote
from agent_ops.workflows.review import DEFAULT_JOBS, FAILED_STATUSES
from agent_ops.workflows.spawn import REPORT_STATES
from agent_ops.workflows.triage import LABEL_COLORS

app = typer.Typer(
    name="agent",
    help="agent-ops: orchestrate agentic SDLC workflows across your repos.",
    no_args_is_help=True,
)
worktree_app = typer.Typer(help="Manage per-task worktrees.", no_args_is_help=True)
app.add_typer(worktree_app, name="worktree")

ProjectOpt = Annotated[
    Path, typer.Option("--project", "-C", help="Project repo root (default: cwd)")
]


def _err(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


GITIGNORE_MARKERS = (".worktrees/", ".agent-runs/")

# Every label the lanes read or write, with the colors the pipelines use.
# The triage/groom/scout lanes create their own verdict labels at run time
# (LABEL_COLORS, merged in below), but the gate labels are applied by a human
# — they have to exist before anyone can request work, so `init` prints the
# whole set at onboarding rather than leaving it to the first failed run.
ONBOARDING_LABELS: dict[str, str] = {
    "spec-requested": "5319e7",
    "plan-requested": "1d76db",
    "approved-for-agent": "1d76db",
    "blocked": "b60205",
    "triage:done": "ededed",
    "ready-to-merge": "0e8a16",
    "hotfix-ready": "d93f0b",
    "hotfix-backmerge": "5319e7",
    **LABEL_COLORS,
}


def _missing_gitignore_markers(root: Path) -> list[str]:
    """Markers absent from the project's .gitignore (all of them if it has none)."""
    gitignore = root / ".gitignore"
    text = gitignore.read_text() if gitignore.exists() else ""
    return [marker for marker in GITIGNORE_MARKERS if marker not in text]


def _stub_label(stub: Path) -> Path:
    """A shipped stub as `stubs/managed-repo-<lane>.yml`, anything else in full.

    relative_to raises for a stub outside PLATFORM_ROOT, which only happens
    when it's been pointed elsewhere — fall back to the full path rather than
    turning a warning into a traceback.
    """
    try:
        return stub.relative_to(PLATFORM_ROOT)
    except ValueError:
        return stub


def _report_caller_drift(root: Path) -> None:
    """Report stub drift for every CI lane the repo has a caller for.

    A summary line names the lanes checked and the ones the repo hasn't wired
    up (not opting into a lane isn't drift), then one line per lane that
    actually drifted — so six lanes stay as scannable as one, and a clean repo
    is a single line.

    Goes through the `stubs` module attribute rather than a from-import so
    tests can repoint stubs.STUBS_DIR and have the check follow it.
    """
    results = stubs.caller_drift(root)
    if not results:
        return  # no agent-ops lanes wired up at all — nothing to compare

    problems: list[str] = []
    for drift in results:
        if drift.error:
            problems.append(f"! {drift.lane} drift check skipped: {drift.error}")
        elif drift.secrets or drift.permissions:
            parts = []
            if drift.secrets:
                parts.append(f"secrets: {', '.join(drift.secrets)}")
            if drift.permissions:
                parts.append(f"permissions: {', '.join(drift.permissions)}")
            caller = drift.path or stubs.WORKFLOWS_REL
            problems.append(
                f"! {caller} is behind {_stub_label(drift.stub)} — missing {'; '.join(parts)}"
            )

    checked = {drift.lane for drift in results}
    absent = [lane for lane in stubs.known_lanes() if lane not in checked]
    tail = f" (not wired: {', '.join(absent)})" if absent else ""
    lanes = ", ".join(drift.lane for drift in results)
    typer.echo(
        f"! CI lane callers checked: {lanes}{tail}"
        if problems
        else f"✓ CI lane callers in sync: {lanes}{tail}"
    )
    for line in problems:
        typer.echo(line)


def _checkout_drift(root: Path) -> str | None:
    """Warn when `root` (the editable install's tree) is behind/ahead of its upstream.

    Uses only already-fetched refs — never runs `git fetch` — so `doctor` stays a
    fast, network-free diagnostic. Silent (returns None) for anything that isn't a
    plain, tracked, attached-HEAD git checkout, so it never tracebacks.
    """
    try:
        if not root.is_dir():
            return None
        toplevel = run(["git", "rev-parse", "--show-toplevel"], cwd=root, check=False)
        if toplevel.returncode != 0 or Path(toplevel.stdout.strip()) != root.resolve():
            return None
        head = run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cwd=root, check=False)
        if head.returncode != 0:
            return None
        upstream_name = run(
            ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            cwd=root,
            check=False,
        )
        if upstream_name.returncode != 0:
            return None
        upstream = upstream_name.stdout.strip()
        counts = run(
            ["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"],
            cwd=root,
            check=False,
        )
        if counts.returncode != 0:
            return None
        parts = counts.stdout.strip().split()
        if len(parts) != 2:
            return None
        ahead, behind = int(parts[0]), int(parts[1])
        if ahead == 0 and behind == 0:
            return None

        def commits(n: int) -> str:
            return f"{n} commit{'' if n == 1 else 's'}"

        if behind and not ahead:
            return (
                f"agent-ops checkout is {commits(behind)} behind {upstream} — the editable "
                f"install runs this tree; git -C {root} pull "
                "(local refs; run git fetch for a current comparison)"
            )
        if ahead and not behind:
            return f"agent-ops checkout is {commits(ahead)} ahead of {upstream} ({root})"
        return (
            f"agent-ops checkout has diverged from {upstream}: {commits(ahead)} ahead, "
            f"{commits(behind)} behind ({root}) "
            "(local refs; run git fetch for a current comparison)"
        )
    except Exception:  # noqa: BLE001 — doctor reports, never crashes
        return None


@app.command()
def implement(
    issue: Annotated[int, typer.Argument(help="GitHub issue number to implement")],
    project: ProjectOpt = Path("."),
    runtime: Annotated[str | None, typer.Option(help="Override runtime")] = None,
    no_pr: Annotated[bool, typer.Option("--no-pr", help="Skip push + PR creation")] = False,
    keep_worktree: Annotated[bool, typer.Option(help="Keep worktree after success")] = False,
    plan_file: Annotated[
        Path | None,
        typer.Option("--plan-file", help="Use this approved plan instead of running the planner"),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Implement even if an open PR already references it")
    ] = False,
) -> None:
    """Implement a GitHub issue: worktree → agent loop → gates → self-review → PR."""
    try:
        ok = run_implement(
            project.resolve(),
            issue,
            runtime_name=runtime,
            open_pr=not no_pr,
            keep_worktree=keep_worktree,
            plan_file=plan_file,
            force=force,
        )
    except (CommandError, FileExistsError, RuntimeError, FileNotFoundError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    raise typer.Exit(0 if ok else 1)


@app.command()
def dispatch(
    issue: Annotated[int, typer.Argument(help="GitHub issue number to implement")],
    project: ProjectOpt = Path("."),
    surface: Annotated[str, typer.Option(help="Where to run: auto | orca | background")] = "auto",
    no_pr: Annotated[bool, typer.Option("--no-pr", help="Skip push + PR creation")] = False,
    plan_file: Annotated[
        Path | None,
        typer.Option("--plan-file", help="Use this approved plan instead of running the planner"),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Dispatch even if an open PR already references it")
    ] = False,
) -> None:
    """Spawn `agent implement` on a visible surface (Orca terminal, background log, ...)."""
    root = project.resolve()
    command = ["agent", "implement", str(issue), "--project", str(root)]
    if no_pr:
        command.append("--no-pr")
    if plan_file:
        # Absolute: the surface may spawn the command from the worktree rather
        # than the caller's cwd, and a relative plan path would resolve wrong.
        command.extend(["--plan-file", str(plan_file.resolve())])
    if force:
        command.append("--force")

    # Pre-create the worktree implement will reuse, so the surface can attach
    # the run to the issue's worktree card instead of the project root's.
    try:
        # Check before the worktree exists — implement would only fail after
        # dispatch had already created one, leaving it behind to clean up.
        if plan_file and not plan_file.is_file():
            raise CommandError(f"plan file not found: {plan_file}")
        if not force:
            existing = github.open_prs_for_issue(issue, cwd=root)
            if existing:
                pr = existing[0]
                raise CommandError(
                    f"issue #{issue} already has open PR #{pr['number']} ({pr['url']}) — "
                    "review/merge that instead, or close it to re-dispatch. "
                    "Pass --force to override."
                )
        chosen = surfaces.pick(surface)
        config = load_project_config(root)
        task_id, branch = task_identifiers(issue)
        wt_path = worktree.create(
            root, config.worktree_dir, task_id, branch, config.base_branch, reuse=True
        )
    except (ValueError, FileExistsError, CommandError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc

    try:
        spawned = chosen.spawn(f"agent-issue-{issue}", command, root, attach_path=wt_path)
    except CommandError as exc:
        # The worktree itself is fine — only the surface attach (e.g. Orca not
        # yet indexing the brand-new worktree) failed. Keep the worktree and
        # branch: deleting them here would force a retry to recreate the
        # worktree from scratch and race the exact same window again.
        _err(
            f"worktree {wt_path} was created successfully, but attaching the run to a "
            f"surface failed: {exc}\n"
            f"the worktree and branch were kept — re-run `agent dispatch {issue} "
            f"--project {root}` to retry, it will reuse this worktree.\n"
            f"or attach manually: agent implement {issue} --project {root}"
        )
        raise typer.Exit(1) from exc
    # How to reach the run once it is going, so `agent runs --wait` can be woken
    # by it instead of inferring its state from the outside (issue #98).
    messages.record_spawn(
        root,
        issue,
        surface=spawned.surface,
        handle=spawned.handle,
        pid=spawned.pid,
        log_path=spawned.log_path,
        log=_err,
    )
    typer.echo(f"dispatched issue #{issue} → {spawned.where}")


@app.command()
def spawn(
    issue: Annotated[int, typer.Argument(help="GitHub issue number the work belongs to")],
    project: ProjectOpt = Path("."),
    prompt: Annotated[
        str | None,
        typer.Option("--prompt", "-m", help="Opening brief (default: 'work on issue #N')"),
    ] = None,
    prompt_file: Annotated[
        Path | None, typer.Option("--prompt-file", help="File containing the opening brief")
    ] = None,
    surface: Annotated[str, typer.Option(help="Where to run: auto | orca | background")] = "auto",
    runtime: Annotated[str | None, typer.Option(help="Override runtime")] = None,
    permission_mode: Annotated[
        str | None,
        typer.Option(
            "--permission-mode",
            # The valid set is per-runtime, so it is not spelled out here: the
            # runtime rejects an unknown mode by name before anything is built.
            help="Override runtime.interactive_permission_mode for this spawn "
            "(claude: bypassPermissions | acceptEdits | plan | ...)",
        ),
    ] = None,
) -> None:
    """Put an interactive coding agent in a fresh worktree, wired to report when it stops.

    The ad-hoc counterpart to `dispatch`: that one spawns the `agent implement`
    pipeline, this one spawns a plain agent session for work the pipeline does
    not model — and seeds a stop hook so its completion is reported even if it
    dies, is interrupted, or stops early (issue #113). Wait on it with
    `agent runs <issue> --wait`.

    The session runs at `runtime.interactive_permission_mode` — high by default,
    because a delegated worker that stops to ask has nobody to ask (issue #115).
    Tighten it for one spawn with `--permission-mode`.
    """
    root = project.resolve()
    try:
        if prompt and prompt_file:
            raise CommandError("pass either --prompt or --prompt-file, not both")
        if prompt_file is not None:
            if not prompt_file.is_file():
                raise CommandError(f"prompt file not found: {prompt_file}")
            prompt = prompt_file.read_text()
        delegated = run_spawn(
            root,
            issue,
            prompt=prompt,
            surface_name=surface,
            runtime_name=runtime,
            permission_mode=permission_mode,
            log=typer.echo,
        )
    except (ValueError, FileExistsError, CommandError, RuntimeError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc

    typer.echo(f"spawned an agent for issue #{issue} → {delegated.spawned.where}")
    typer.echo(f"  worktree: {delegated.worktree}")
    typer.echo(
        f"  reports back on its own — wait with `agent runs {issue} --wait`"
        if delegated.reports_back
        else f"  no push channel — watch it with `agent runs {issue} --wait` (polling)"
    )


@app.command()
def report(
    issue: Annotated[int, typer.Argument(help="GitHub issue number this run belongs to")],
    state: Annotated[
        str, typer.Option("--state", help=f"Terminal state: {' | '.join(REPORT_STATES)}")
    ],
    project: ProjectOpt = Path("."),
    pr: Annotated[str | None, typer.Option("--pr", help="URL of the PR this run opened")] = None,
    reason: Annotated[
        str | None, typer.Option("--reason", help="Why it ended this way (blocker, failure, ...)")
    ] = None,
    if_unreported: Annotated[
        bool,
        typer.Option(
            "--if-unreported",
            help="Do nothing if this run already reported — what the stop hook passes",
        ),
    ] = False,
) -> None:
    """Record and push this run's terminal state, waking anyone blocked on it.

    Callable by hand (an agent reporting a real outcome, with a PR or a
    blocker) and by the stop hook `agent spawn` seeds, which passes
    `--if-unreported` so a worker that already spoke for itself is never
    overwritten by the generic report.

    Always exits 0. It is run from a Claude Code `Stop` hook, where a non-zero
    exit is fed back to the agent as something to fix — a status report has no
    business doing that, and a report that could not go out is a dropped
    notification, not a failed run.
    """
    if state not in REPORT_STATES:
        _err(f"unknown state {state!r} — expected one of: {', '.join(REPORT_STATES)}")
        return
    try:
        report_outcome(
            project.resolve(),
            issue,
            state=state,
            pr_url=pr,
            reason=reason,
            if_unreported=if_unreported,
            log=typer.echo,
        )
    except Exception as exc:  # noqa: BLE001 — a status report never fails a run
        _err(f"could not report #{issue}: {exc}")


@app.command()
def resume(
    issue: Annotated[int, typer.Argument(help="GitHub issue number to resume")],
    project: ProjectOpt = Path("."),
    message: Annotated[
        str | None, typer.Option("--message", "-m", help="Feedback for the agent")
    ] = None,
    message_file: Annotated[
        Path | None,
        typer.Option("--message-file", help="File containing feedback for the agent"),
    ] = None,
    runtime: Annotated[str | None, typer.Option(help="Override runtime")] = None,
    no_pr: Annotated[bool, typer.Option("--no-pr", help="Skip push + PR creation")] = False,
    keep_worktree: Annotated[bool, typer.Option(help="Keep worktree after success")] = False,
    surface: Annotated[
        str,
        typer.Option(help="Where to run: auto | orca | background | inline"),
    ] = "auto",
) -> None:
    """Run an agent in an existing task worktree — the resume path after a self-review halt."""
    root = project.resolve()

    # inline is not the default here (unlike plan/review): resume's point is
    # to be attached to a visible surface the same way dispatch is.
    if surface == "inline":
        try:
            ok = run_resume(
                root,
                issue,
                message=message,
                message_file=message_file,
                runtime_name=runtime,
                open_pr=not no_pr,
                keep_worktree=keep_worktree,
            )
        except (CommandError, FileNotFoundError, RuntimeError, ValueError) as exc:
            _err(str(exc))
            raise typer.Exit(1) from exc
        raise typer.Exit(0 if ok else 1)

    try:
        where = dispatch_resume(
            root,
            issue,
            surface_name=surface,
            message=message,
            message_file=message_file,
            runtime_name=runtime,
            open_pr=not no_pr,
            keep_worktree=keep_worktree,
        )
    except (CommandError, FileNotFoundError, RuntimeError, ValueError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"resumed issue #{issue} → {where.where}")


@app.command()
def plan(
    issue: Annotated[int, typer.Argument(help="GitHub issue number to plan")],
    project: ProjectOpt = Path("."),
    runtime: Annotated[str | None, typer.Option(help="Override runtime")] = None,
    post: Annotated[bool, typer.Option("--post", help="Post the plan as an issue comment")] = False,
    surface: Annotated[
        str,
        typer.Option(
            help="Where to run: inline (print here) | auto | orca | background",
        ),
    ] = "inline",
) -> None:
    """Run only the planner role (smart model, read-only) and print the plan."""
    root = project.resolve()

    # inline is the default on purpose: a plan's whole value is the text it
    # prints, and plan-pipeline.yml consumes it inline on the runner.
    if surface != "inline":
        try:
            where = dispatch_plan(
                root, issue, surface_name=surface, post_comment=post, runtime_name=runtime
            )
        except (ValueError, CommandError) as exc:
            _err(str(exc))
            raise typer.Exit(1) from exc
        typer.echo(f"dispatched plan for issue #{issue} → {where.where}")
        return

    config = load_project_config(root)
    try:
        issue_data = github.get_issue(issue, cwd=root)
        request, result = make_plan(
            config, issue_data, root, runtime_override=runtime, log=typer.echo
        )
    except (CommandError, RuntimeError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(result.text)
    if post:
        body = f"## Agent plan\n\n{result.text}{artifact_footer(request, result)}"
        github.comment_on_issue(issue, body, cwd=root)
        typer.echo(f"posted plan on issue #{issue}")


@app.command()
def spec(
    issue: Annotated[int, typer.Argument(help="GitHub issue number to elaborate")],
    project: ProjectOpt = Path("."),
    runtime: Annotated[str | None, typer.Option(help="Override runtime")] = None,
    post: Annotated[
        bool, typer.Option("--post/--no-post", help="Post the spec as an issue comment")
    ] = True,
) -> None:
    """Elaborate a backlog idea into an agent-ready spec (checklist acceptance criteria)."""
    from agent_ops.workflows.spec import run_spec

    try:
        text = run_spec(project.resolve(), issue, post=post, runtime_override=runtime)
    except (CommandError, RuntimeError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(text)


@app.command()
def review(
    prs: Annotated[list[int] | None, typer.Argument(help="PR number(s) to review")] = None,
    project: ProjectOpt = Path("."),
    runtime: Annotated[str | None, typer.Option(help="Override runtime")] = None,
    post: Annotated[bool, typer.Option("--post", help="Post the review as a PR comment")] = False,
    all_open: Annotated[
        bool,
        typer.Option("--all", help="Review every open PR targeting the project's base_branch"),
    ] = False,
    jobs: Annotated[
        int, typer.Option("--jobs", help="Max concurrent reviews when reviewing multiple PRs")
    ] = DEFAULT_JOBS,
    surface: Annotated[
        str,
        typer.Option(
            help="Where to run: inline (print here) | auto | orca | background",
        ),
    ] = "inline",
) -> None:
    """Run a read-only review agent over one or more PR diffs."""
    root = project.resolve()

    if all_open and prs:
        _err("pass either PR numbers or --all, not both")
        raise typer.Exit(1)
    if not all_open and not prs:
        _err("specify at least one PR number, or --all")
        raise typer.Exit(1)

    if all_open:
        config = load_project_config(root)
        try:
            pr_numbers = github.open_pr_numbers(config.base_branch, cwd=root)
        except CommandError as exc:
            _err(str(exc))
            raise typer.Exit(1) from exc
        if not pr_numbers:
            typer.echo(f"no open PRs targeting {config.base_branch!r}")
            return
    else:
        assert prs is not None  # guarded above
        pr_numbers = prs

    # inline is the default on purpose: scripted callers and the docs' usage
    # (`agent review 45 --post`) consume the review text on stdout.
    if surface != "inline":
        try:
            where = dispatch_review(
                root, pr_numbers, surface_name=surface, post_comment=post, runtime_name=runtime
            )
        except (ValueError, CommandError) as exc:
            _err(str(exc))
            raise typer.Exit(1) from exc
        label = f"PR #{pr_numbers[0]}" if len(pr_numbers) == 1 else f"{len(pr_numbers)} PRs"
        typer.echo(f"dispatched review of {label} → {where.where}")
        return

    if len(pr_numbers) == 1:
        # Single-PR inline path is unchanged: no summary, just the review text.
        try:
            text = run_review(root, pr_numbers[0], runtime_name=runtime, post_comment=post)
        except (CommandError, RuntimeError) as exc:
            _err(str(exc))
            raise typer.Exit(1) from exc
        typer.echo(text)
        return

    outcomes = run_reviews(
        root, pr_numbers, jobs=jobs, post_comment=post, runtime_name=runtime, log=typer.echo
    )
    for outcome in outcomes:
        if outcome.text:
            typer.echo(f"\n=== PR #{outcome.pr} ===\n{outcome.text}")
    typer.echo("\n" + format_summary(outcomes))
    if any(o.status in FAILED_STATUSES for o in outcomes):
        raise typer.Exit(1)


@app.command()
def triage(
    project: ProjectOpt = Path("."),
    dispatch: Annotated[
        bool, typer.Option("--dispatch", help="Spawn implement runs for agent-ready issues")
    ] = False,
) -> None:
    """Classify untriaged open issues (agent-ready / needs-human / backlog) and label them."""
    from agent_ops.workflows.triage import run_triage

    try:
        results = run_triage(project.resolve(), dispatch=dispatch)
    except (CommandError, RuntimeError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    counts: dict[str, int] = {}
    for r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    typer.echo(", ".join(f"{v}: {n}" for v, n in counts.items()) or "nothing to triage")


@app.command()
def groom(project: ProjectOpt = Path(".")) -> None:
    """Re-validate open issues: close fixed/invalid ones, promote workable ones to agent-ready."""
    from agent_ops.workflows.groom import run_groom

    try:
        results = run_groom(project.resolve())
    except (CommandError, RuntimeError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    counts: dict[str, int] = {}
    for r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    typer.echo(", ".join(f"{v}: {n}" for v, n in counts.items()) or "nothing to groom")


@app.command()
def scout(
    project: ProjectOpt = Path("."),
    max_issues: Annotated[int, typer.Option("--max", help="Maximum number of issues to file")] = 3,
) -> None:
    """Mine TODOs, deferred review threads, and gaps; file capped backlog issues."""
    from agent_ops.workflows.scout import run_scout

    try:
        results = run_scout(project.resolve(), max_issues=max_issues)
    except (CommandError, RuntimeError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"filed {len(results)} issue(s)" if results else "nothing filed")


@app.command()
def merge(
    pr: Annotated[int, typer.Argument(help="PR number to merge into the working branch")],
    project: ProjectOpt = Path("."),
    override: Annotated[
        bool, typer.Option("--override", help="Human override: merge despite rule violations")
    ] = False,
) -> None:
    """Squash-merge a PR into the working branch (staging) if all merge rules pass."""
    try:
        ok = run_merge(project.resolve(), pr, override=override)
    except CommandError as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    raise typer.Exit(0 if ok else 1)


@app.command()
def promote(project: ProjectOpt = Path(".")) -> None:
    """Open the staging → stable promotion PR for human verification (never merges)."""
    try:
        run_promote(project.resolve())
    except CommandError as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc


@app.command()
def init(project: ProjectOpt = Path(".")) -> None:
    """Scaffold .agent/config.yaml, AGENTS.md, and a CLAUDE.md link into a project repo."""
    root = project.resolve()
    templates = PLATFORM_ROOT / "templates" / "project"

    config_dst = root / PROJECT_CONFIG_REL
    if config_dst.exists():
        typer.echo(f"skip: {config_dst} already exists")
    else:
        config_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(templates / "config.yaml", config_dst)
        (config_dst.parent / "skills").mkdir(exist_ok=True)
        typer.echo(f"wrote {config_dst}")

    agents_dst = root / "AGENTS.md"
    if agents_dst.exists():
        typer.echo(f"skip: {agents_dst} already exists")
    else:
        shutil.copy(templates / "AGENTS.md", agents_dst)
        typer.echo(f"wrote {agents_dst}")

    # AGENTS.md is canonical; CLAUDE.md is a symlink so Claude Code and other
    # runtimes read the same project instructions without duplication.
    claude_dst = root / "CLAUDE.md"
    if claude_dst.exists() or claude_dst.is_symlink():
        typer.echo(f"skip: {claude_dst} already exists")
    else:
        claude_dst.symlink_to("AGENTS.md")
        typer.echo("linked CLAUDE.md -> AGENTS.md")

    # issue template: nudges checklist acceptance criteria at capture time,
    # which is what the triage/groom UI bar keys off
    template_dst = root / ".github" / "ISSUE_TEMPLATE" / "task.md"
    if template_dst.exists():
        typer.echo(f"skip: {template_dst} already exists")
    else:
        template_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(templates / "issue-template-task.md", template_dst)
        typer.echo(f"wrote {template_dst}")

    gitignore = root / ".gitignore"
    for marker in _missing_gitignore_markers(root):
        with gitignore.open("a") as fh:
            fh.write(f"\n{marker}\n")
        typer.echo(f"added {marker} to .gitignore")

    # Labels live in GitHub, not on disk, so init can only tell you about them
    # — but it has to, because a gate label nobody created is a lane nobody can
    # request: the CI pipelines select on it, and a missing label just selects
    # nothing.
    typer.echo("\nlabels the lanes use — run once per repo:")
    for name, color in ONBOARDING_LABELS.items():
        typer.echo(f"  gh label create {name} --color {color} --force")


@app.command()
def doctor(project: ProjectOpt = Path(".")) -> None:
    """Check that required CLIs are installed and the project config is valid."""
    ok = True
    config: ProjectConfig | None = None
    config_error: str | None = None
    try:
        config = load_project_config(project.resolve())
    except Exception as exc:  # noqa: BLE001 — doctor reports, never crashes
        config_error = str(exc)

    for tool in ("git", "gh"):
        found = shutil.which(tool) is not None
        typer.echo(f"{'✓' if found else '✗'} {tool}{'' if found else ' (missing)'}")
        ok = ok and found

    # Runtime CLIs answer for themselves through the registry, rather than this
    # command carrying a list of vendor binary names: a new adapter shows up
    # here without `doctor` learning anything about it. Only the configured
    # runtime is required — an install that never reaches for `--runtime codex`
    # should not be told it is unhealthy for not having Codex.
    for name in runtime_names():
        if get_runtime(name).available():
            typer.echo(f"✓ {name}")
        elif config is not None and name == config.runtime.name:
            typer.echo(f"✗ {name} (missing; it is this project's runtime.name)")
            ok = False
        else:
            typer.echo(f"- {name} (optional)")

    if config is None:
        _err(f"✗ config error: {config_error}")
        ok = False
    else:
        typer.echo(
            f"✓ config valid (runtime: {config.runtime.name}, gates: "
            f"{', '.join(config.loop.gates)})"
        )
        unset = [g for g in config.loop.gates if not getattr(config.commands, g, None)]
        if unset:
            typer.echo(f"! gates with no command configured (will be skipped): {', '.join(unset)}")
        ok = _report_roles(config) and ok
        for warning in ladder_warnings(config):
            typer.echo(f"! {warning}")

    missing = _missing_gitignore_markers(project.resolve())
    if missing:
        typer.echo(f"! .gitignore missing {', '.join(missing)} — run: agent init")

    _report_caller_drift(project.resolve())
    checkout_note = _checkout_drift(PLATFORM_ROOT)
    if checkout_note:
        typer.echo(f"! {checkout_note}")

    raise typer.Exit(0 if ok else 1)


def _report_roles(config: ProjectConfig) -> bool:
    """Print what each role resolves to, per runtime. False if a run cannot start.

    Two sections, because the two questions have different stakes. The runtimes
    the project actually uses are checked first, and a role that cannot resolve
    a model there is a failure: that run will not start.

    Every other registered runtime is then reported as a *warning*. A tier the
    effective runtime does not define refuses at resolution rather than handing
    a foreign model name to a CLI (#39) — correct, but only discoverable today
    by running with the override, and people reach for `--runtime` precisely
    when the usual one has stopped working. Not having a table for a runtime
    you never use is not a fault, so it never fails the check.
    """
    ok = True
    reports = role_reports(config)
    for report in reports:
        if report.error:
            _err(f"✗ {report.name}: {report.error}")
            ok = False
            continue
        ladder = " → ".join(report.fallbacks) if report.fallbacks else "none configured"
        typer.echo(
            f"  {report.name}: {report.runtime} / "
            f"{report.model or 'runtime default'} (fallbacks: {ladder})"
        )

    in_use = {report.runtime for report in reports}
    others = [name for name in runtime_names() if name not in in_use]
    for other in runtime_reports(config, others):
        gaps = other.missing_tiers()
        if gaps:
            detail = "; ".join(
                f"no {tier!r} for {', '.join(roles)}" for tier, roles in sorted(gaps.items())
            )
            typer.echo(
                f"! --runtime {other.runtime} would be refused — "
                f"model_tiers.{other.runtime} has {detail}"
            )
            continue
        resolved = ", ".join(
            f"{role.name} {role.model or 'runtime default'}" for role in other.roles
        )
        typer.echo(f"  --runtime {other.runtime}: {resolved}")
    return ok


@app.command()
def queue(
    project: ProjectOpt = Path("."),
    label: Annotated[
        str, typer.Option(help="Label that marks issues as ready for an agent")
    ] = "agent-ready",
) -> None:
    """List open issues labeled ready for the agent, oldest first."""
    try:
        proc = run(
            [
                "gh",
                "issue",
                "list",
                "--state",
                "open",
                "--label",
                label,
                "--json",
                "number,title",
                "--limit",
                "20",
            ],
            cwd=project.resolve(),
        )
    except CommandError as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc

    issues = json.loads(proc.stdout)
    if not issues:
        typer.echo(f"no open issues labeled {label!r}")
        return
    for issue in reversed(issues):  # gh lists newest first; work oldest first
        typer.echo(f"#{issue['number']}\t{issue['title']}")


@app.command()
def status(
    pipelines: Annotated[
        bool,
        typer.Option(
            "--pipelines",
            help="Show per-repo CI lane coverage (triage/groom/...) instead of PRs and issues",
        ),
    ] = False,
    failures: Annotated[
        bool,
        typer.Option(
            "--failures",
            help="Show recent failed workflow runs for every registered repo",
        ),
    ] = False,
    sync_orca: Annotated[
        bool,
        typer.Option(
            "--sync-orca",
            help="Mirror active agent lanes onto Orca worktree cards (read-only towards GitHub)",
        ),
    ] = False,
) -> None:
    """Fleet overview: open PRs and issue buckets for every registered repo."""
    from agent_ops.status import fleet_failures, fleet_status, pipeline_coverage

    chosen = [
        flag
        for flag, on in (
            ("--pipelines", pipelines),
            ("--failures", failures),
            ("--sync-orca", sync_orca),
        )
        if on
    ]
    if len(chosen) > 1:
        _err(f"{' and '.join(chosen)} are separate views; pass one at a time")
        raise typer.Exit(1)
    try:
        config = registry.load_registry()
        if pipelines:
            pipeline_coverage(config)
        elif failures:
            fleet_failures(config)
        elif sync_orca:
            from agent_ops.orca_sync import sync_orca as run_sync

            run_sync(config)
        else:
            fleet_status(config)
    except (CommandError, FileNotFoundError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc


@app.command()
def runs(
    issue: Annotated[int | None, typer.Argument(help="Show/wait on only this issue")] = None,
    project: ProjectOpt = Path("."),
    wait: Annotated[
        bool,
        typer.Option("--wait", "-w", help="Block until every tracked run reaches a terminal state"),
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to wait before giving up (0 = no bound)")
    ] = 3600.0,
    interval: Annotated[
        float,
        typer.Option("--interval", help="Seconds between polls while waiting (floor: 1s)"),
    ] = 15.0,
) -> None:
    """Per-issue run state — running / halted / stopped / done — derived from
    worktrees, `.agent-runs/` feedback files and open PRs. No Orca dependency.

    With --wait, blocks and prints transitions until every tracked run (or
    just `issue`, if given) reaches a terminal state."""
    from agent_ops.runs import report_runs, wait_for_runs

    root = project.resolve()
    try:
        if not wait:
            report_runs(root, issue=issue)
            return
        timeout_s = None if timeout <= 0 else timeout
        if wait_for_runs(root, issue=issue, timeout_s=timeout_s, interval_s=interval):
            return
        target = f"#{issue}" if issue is not None else "runs"
        _err(f"timed out after {timeout:g}s waiting for {target}")
        raise typer.Exit(1)
    except (CommandError, FileNotFoundError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc


@app.command()
def runtimes() -> None:
    """List available runtimes and whether their CLI is installed."""
    for name in runtime_names():
        rt = get_runtime(name)
        typer.echo(f"{'✓' if rt.available() else '✗'} {name}")


@app.command()
def version() -> None:
    typer.echo(__version__)


@worktree_app.command("list")
def worktree_list(project: ProjectOpt = Path(".")) -> None:
    for wt in worktree.list_worktrees(project.resolve()):
        typer.echo(f"{wt.branch}\t{wt.path}")


@worktree_app.command("remove")
def worktree_remove(
    task_id: Annotated[str, typer.Argument(help="Task id, e.g. issue-123")],
    project: ProjectOpt = Path("."),
    force: Annotated[bool, typer.Option("--force", help="Remove even if dirty")] = False,
    delete_branch: Annotated[
        bool,
        typer.Option(
            "--delete-branch",
            help="Also delete the local branch the worktree was on "
            "(unmerged branches are kept unless --force)",
        ),
    ] = False,
) -> None:
    config = load_project_config(project.resolve())
    try:
        worktree.remove(
            project.resolve(),
            config.worktree_dir,
            task_id,
            force=force,
            delete_branch=delete_branch,
        )
    except CommandError as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"removed {task_id}")


if __name__ == "__main__":
    app()
