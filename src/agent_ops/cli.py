from __future__ import annotations

import json
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from agent_ops import (
    __version__,
    claims,
    github,
    grants,
    messages,
    prompts,
    registry,
    stubs,
    surfaces,
    worktree,
)

# Aliased: this module defines its own `runs` typer command further down, and
# that `def` rebinds the plain name at module scope — importing the module
# itself unaliased would be silently shadowed by it.
from agent_ops import runs as runs_mod
from agent_ops.config import (
    PROJECT_CONFIG_REL,
    ROLE_NAMES,
    ProjectConfig,
    ladder_warnings,
    load_project_config,
    role_reports,
    runtime_reports,
)
from agent_ops.fallback import artifact_footer
from agent_ops.gates import missing_requirements
from agent_ops.github import Label
from agent_ops.runs import issue_from_branch
from agent_ops.runtimes import get_runtime, runtime_names
from agent_ops.runtimes.credentials import capture_ci_credentials
from agent_ops.status import STAGE_CONSUMERS, local_deployed_lanes
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
from agent_ops.workflows.merge import (
    batch_candidates,
    run_merge,
    run_merge_batch,
    run_merge_check,
    run_promote,
)
from agent_ops.workflows.review import DEFAULT_JOBS, FAILED_STATUSES, ReviewOutcome
from agent_ops.workflows.spawn import REPORT_STATES
from agent_ops.workflows.triage import GATE_LABELS, LABEL_COLORS

app = typer.Typer(
    name="agent",
    help="agent-ops: orchestrate agentic SDLC workflows across your repos.",
    # Bare `agent` opens the pipeline TUI (see the callback below) rather than
    # printing help — `no_args_is_help` would pre-empt that callback.
    no_args_is_help=False,
)
worktree_app = typer.Typer(help="Manage per-task worktrees.", no_args_is_help=True)
app.add_typer(worktree_app, name="worktree")

ProjectOpt = Annotated[
    Path, typer.Option("--project", "-C", help="Project repo root (default: cwd)")
]


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context) -> None:
    """agent-ops: orchestrate agentic SDLC workflows across your repos.

    Run with no command to open the pipeline TUI (issue #232) — the one
    screen for pipeline/runs/PRs, with dispatch and resume, and every
    keybinding showing the command it runs.
    """
    if ctx.invoked_subcommand is None:
        from agent_ops.tui import run_tui

        try:
            path, sink_error = run_tui(Path("."))
        except (ValueError, CommandError) as exc:
            _err(str(exc))
            raise typer.Exit(1) from exc
        if sink_error:
            _err(f"! {sink_error}; wrote {path} instead")
        if path is not None:
            typer.echo(str(path))


def _err(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


GITIGNORE_MARKERS = (".worktrees/", ".agent-runs/")

# Every label the lanes read or write, with the colors/descriptions the
# pipelines use. The triage/groom/scout lanes sync their own verdict labels at
# run time too (LABEL_COLORS, merged in below, plus the gate labels groom may
# now emit), but a gate label may be applied by a human or by groom — it has
# to exist before anyone can request work, so `init` syncs the whole set at
# onboarding rather than leaving it to the first failed run.
ONBOARDING_LABELS: dict[str, Label] = {
    **GATE_LABELS,
    "approved-for-agent": Label(
        "1d76db", "Human go-ahead for an enhancement/idea to enter the normal lane"
    ),
    "blocked": Label("b60205", "Skipped by every lane until a human clears the blocker"),
    "ready-to-merge": Label("0e8a16", "Passed every gate; held for merge (report-only mode)"),
    "hotfix-ready": Label("d93f0b", "Hotfix passed PASS+APPROVE; held for maintainer review"),
    "hotfix-backmerge": Label("5319e7", "Back-merge PR carrying a hotfix from stable to base"),
    # Applied and cleared by the runs themselves, not by a human — but listed
    # here anyway: an agent started by hand claims with `agent claim <N>`, and a
    # repo where the label does not exist yet turns that into a failed claim
    # rather than a visible one.
    claims.CLAIM_LABEL: Label(claims.CLAIM_LABEL_COLOR, claims.CLAIM_LABEL_DESCRIPTION),
    **LABEL_COLORS,
}


def _print_label_commands() -> None:
    for name, label in ONBOARDING_LABELS.items():
        typer.echo(
            f"  gh label create {name} --color {label.color} "
            f'--description "{label.description}" --force'
        )


def _lane_copy_command(lane: str, root: Path) -> str:
    """The `cp` command that deploys `lane`'s stub caller into `root`.

    Goes through `stubs.stub_for` for the source so a lane whose shipped stub
    is `.yaml` rather than `.yml` still resolves to a real file.
    """
    stub = stubs.stub_for(lane)
    dest = root / ".github" / "workflows" / f"{lane}{stub.suffix}"
    return f"  cp {stub} {dest}"


def _warn_unserviced_labels(root: Path, *, labels_synced: bool) -> None:
    """Warn when this repo's label vocabulary has no deployed CI lane to act
    on it (issue #237/#229/#230): `init` syncs the full onboarding label set
    unconditionally, but deploying the lane caller workflows is a separate,
    easy-to-skip manual step — leaving a repo with the vocabulary of an
    automated repo and none of the machinery.

    `labels_synced` says whether *this run* actually wrote labels to GitHub
    (only true at the one call site right after a successful
    `github.sync_labels`). The other call sites (`--print-labels`, no git
    root, no `origin`, a failed sync) already told the operator on the same
    screen that nothing was synced this run — asserting "labels exist" right
    below that would contradict it, so those paths get wording that only
    claims what this repo's label vocabulary implies, not that this run
    synced anything.

    Warn-only, by design: this never changes `init`'s exit code and never
    blocks the label sync/print above it, so a repo that hasn't wired a lane
    yet still gets its labels and can wire the lane whenever it's ready.

    Explicit decision (issue #237): this does NOT offer to copy the stub
    workflows for you, even though it already knows exactly which ones are
    missing. Each stub needs a real owner filled in, a cadence/runner choice
    that varies per repo, and secrets configured — copying one unattended
    would hand back a workflow broken on its first run, worse than the manual
    step it replaces. `.github/workflows/` writes are also this repo's own
    reviewed danger zone (see AGENTS.md); doing that from `init` without a
    human in the loop is exactly what that policy exists to prevent. So this
    only ever names what's missing and prints the command to fix it.
    """
    lanes = local_deployed_lanes(root)
    if lanes is None:
        typer.echo(
            "! could not read .github/workflows here — can't tell whether any CI lane is "
            "deployed to act on the labels above; run `agent doctor` for a closer look"
        )
        return

    if not lanes:
        all_lanes = sorted({lane for consumers in STAGE_CONSUMERS.values() for lane in consumers})
        if labels_synced:
            typer.echo(
                "\n! labels exist, but no lane workflows are deployed in this checkout — "
                "nothing will act on agent-ready/spec-requested/plan-requested until a stub "
                "is copied:"
            )
        else:
            typer.echo(
                "\n! this repo's label vocabulary implies lane workflows that aren't deployed "
                "in this checkout — nothing will act on agent-ready/spec-requested/"
                "plan-requested until a stub is copied:"
            )
        for lane in all_lanes:
            typer.echo(_lane_copy_command(lane, root))
        typer.echo("  (edit each stub's owner placeholder before using it)")
        return

    for stage, consumers in STAGE_CONSUMERS.items():
        if consumers & lanes:
            continue
        missing = ", ".join(sorted(consumers))
        commands = "; ".join(_lane_copy_command(lane, root).strip() for lane in sorted(consumers))
        typer.echo(f"! `{stage}` has no deployed lane ({missing}) to service it — {commands}")


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

    # An unreadable .github/workflows fails every known lane identically — one
    # line beats one identical "skipped" line per lane. A single lane already
    # gets its own "! <lane> drift check skipped: ..." line below, so only
    # collapse when there's more than one to dedupe.
    distinct_errors = {drift.error for drift in results}
    if len(results) > 1 and len(distinct_errors) == 1 and None not in distinct_errors:
        typer.echo(f"! CI caller drift check skipped: {results[0].error}")
        return

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
    grant_file: Annotated[
        Path | None,
        typer.Option(
            "--grant-file", help="Scoped danger-zone authorization (see docs/trust-model.md)"
        ),
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Implement even if an open PR already references it")
    ] = False,
) -> None:
    """Implement a GitHub issue: worktree → agent loop → gates → self-review → PR."""
    try:
        # CI may hand provider auth to the adapters through one-shot carriers.
        # Consume them before setup or gates can inherit those credentials.
        capture_ci_credentials()
        ok = run_implement(
            project.resolve(),
            issue,
            runtime_name=runtime,
            open_pr=not no_pr,
            keep_worktree=keep_worktree,
            plan_file=plan_file,
            grant_file=grant_file,
            force=force,
        )
    except (CommandError, FileExistsError, RuntimeError, FileNotFoundError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    raise typer.Exit(0 if ok else 1)


@app.command("runtime-preflight")
def runtime_preflight(project: ProjectOpt = Path(".")) -> None:
    """Resolve every configured role and require each runtime CLI before a run."""
    try:
        config = load_project_config(project.resolve())
    except Exception as exc:  # noqa: BLE001 — the diagnostic must name any bad config value
        _err(f"runtime preflight: config invalid: {exc}")
        raise typer.Exit(1) from exc

    ok = True
    checked: set[str] = set()
    for report in role_reports(config):
        if report.error:
            _err(f"runtime preflight: {report.name}: {report.error}")
            ok = False
            continue
        typer.echo(
            f"runtime preflight: {report.name}: {report.runtime} / "
            f"{report.model or 'runtime default'}"
        )
        if report.runtime in checked:
            continue
        checked.add(report.runtime)
        try:
            runtime = get_runtime(report.runtime)
        except ValueError as exc:
            _err(f"runtime preflight: {exc}")
            ok = False
            continue
        if not runtime.available():
            _err(
                f"runtime preflight: {report.runtime} CLI missing "
                f"(required by configured role {report.name})"
            )
            ok = False
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
    grant_file: Annotated[
        Path | None,
        typer.Option(
            "--grant-file", help="Scoped danger-zone authorization (see docs/trust-model.md)"
        ),
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
    if grant_file:
        command.extend(["--grant-file", str(grant_file.resolve())])
    if force:
        command.append("--force")

    # Pre-create the worktree implement will reuse, so the surface can attach
    # the run to the issue's worktree card instead of the project root's.
    try:
        # Check before the worktree exists — implement would only fail after
        # dispatch had already created one, leaving it behind to clean up.
        if plan_file and not plan_file.is_file():
            raise CommandError(f"plan file not found: {plan_file}")
        if grant_file:
            if not grant_file.is_file():
                raise CommandError(f"grant file not found: {grant_file}")
            # Validated here, not just after the backgrounded run picks it up:
            # a grant naming the wrong issue or already expired must fail at
            # the terminal, not silently inside a spawned process (issue #200).
            grants.load(grant_file, issue=issue)
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

    if not wt_path.is_dir():
        # `worktree.create` already enforces this postcondition and would have
        # raised `CommandError` above — this only catches something removing
        # the directory in the window between that check and here, so the
        # common case fails before a terminal is even created (issue #201).
        _err(f"worktree {wt_path} vanished before dispatch could spawn a run for #{issue}")
        raise typer.Exit(1)

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
    # by it instead of inferring its state from the outside (issue #98). Written
    # before the postcondition check below: it is the only address an orphaned
    # terminal has if that check fails (issue #201).
    messages.record_spawn(
        root,
        issue,
        surface=spawned.surface,
        handle=spawned.handle,
        pid=spawned.pid,
        log_path=spawned.log_path,
        # `dispatch`, not `spawn`: the worker here is `agent implement`, which
        # `runs.live_runs` finds in `ps` and whose Orca terminal outlives it —
        # so its handle is an address, never a liveness signal (issue #116).
        kind=messages.DISPATCH_KIND,
        spawner=messages.current_handle(),
        log=_err,
    )

    if not wt_path.is_dir():
        # Dispatch must never claim success over a run with nowhere to work
        # (issue #201): the surface reported a spawn, but the worktree it was
        # supposed to attach to isn't on disk — e.g. it was removed
        # concurrently between the pre-check above and the spawn returning.
        _err(
            f"{spawned.where} was created, but the worktree {wt_path} it should be running in "
            "does not exist — dispatch did NOT produce a worktree. Close or end that terminal "
            f"or process by hand; `agent runs` will show #{issue} as a dead dispatch until then."
        )
        raise typer.Exit(1)

    if spawned.warning:
        _err(f"warning: {spawned.warning}")
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

    if delegated.spawned.warning:
        _err(f"warning: {delegated.spawned.warning}")
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
    pr: Annotated[
        str | None,
        typer.Option("--pr", help="URL of the PR this run opened (a bare number is expanded)"),
    ] = None,
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
    root = project.resolve()
    if pr is not None:
        # Validated here rather than accepted and stored: `pr_url` is read back
        # by `agent runs`, which renders whatever is in it (issue #122).
        # Refusing is safe because the session-end hook never passes `--pr` —
        # only a caller who can read this message and try again does.
        try:
            pr = github.normalise_pr_url(pr, root)
        except CommandError as exc:
            _err(f"{exc} — nothing recorded for #{issue}")
            return
    try:
        report_outcome(
            root,
            issue,
            state=state,
            pr_url=pr,
            reason=reason,
            if_unreported=if_unreported,
            log=typer.echo,
        )
    except Exception as exc:  # noqa: BLE001 — a status report never fails a run
        _err(f"could not report #{issue}: {exc}")


def _current_branch_issue(root: Path) -> int | None:
    """The issue number for `root`'s current branch, or None off an attached
    HEAD that isn't a `fix/issue-N` branch, a detached HEAD, or missing `git`."""
    head = run(["git", "symbolic-ref", "-q", "--short", "HEAD"], cwd=root, check=False)
    if head.returncode != 0:
        return None
    return issue_from_branch(head.stdout.strip())


@app.command()
def claim(
    issue: Annotated[
        int | None, typer.Argument(help="GitHub issue number to claim (omit with --auto)")
    ] = None,
    project: ProjectOpt = Path("."),
    release: Annotated[
        bool, typer.Option("--release", help="Clear the claim instead of taking it")
    ] = False,
    auto: Annotated[
        bool,
        typer.Option(
            "--auto",
            help="Derive the issue from the current fix/issue-N branch; silent no-op otherwise",
        ),
    ] = False,
) -> None:
    """Say an agent is working on this issue right now, so other lanes skip it.

    `agent implement`, `agent resume` and `agent spawn` claim and release on
    their own, and a Claude Code session started by hand in a worktree now
    claims itself too — `agent init` seeds a `SessionStart` hook that runs
    `agent claim --auto`, deriving the issue from the branch name. This is
    what is left for everything that hook does not cover: a non-Claude
    runtime, a hook a user declined, or claiming/releasing by hand.

    `--auto` is the hook's own entry point, not a human one: it exits 0
    whenever there is nothing to claim (branch does not name an issue, `gh`
    fails, no `origin` remote) rather than erroring, because a hook has no one
    to show an error to.

    A claim is not permanent. The CI lane treats one older than the TTL as the
    residue of a dead run and clears it, and `agent doctor` reports stale
    claims — and issues worked locally with no claim at all — long before
    that.
    """
    root = project.resolve()
    if auto:
        if issue is not None:
            _err("`--auto` derives the issue itself; do not pass one")
            raise typer.Exit(1)
        issue = _current_branch_issue(root)
        if issue is None:
            raise typer.Exit(0)
    elif issue is None:
        _err("issue number required (or pass --auto to derive it from the branch)")
        raise typer.Exit(1)

    # Under --auto this is a hook nobody is watching: a `gh` failure or a
    # missing `origin` remote is exactly the "nothing to claim" case above,
    # not a fresh error to surface.
    hook_log = (lambda _msg: None) if auto else _err

    if release:
        ok = claims.release(root, issue, log=hook_log)
        if not auto:
            typer.echo(f"released the claim on #{issue}" if ok else f"#{issue} was not released")
    else:
        ok = claims.claim(root, issue, log=hook_log)
        if not auto:
            typer.echo(
                f"claimed #{issue} — release it with `agent claim {issue} --release`"
                if ok
                else f"#{issue} was not claimed"
            )
    raise typer.Exit(0 if (ok or auto) else 1)


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
    grant_file: Annotated[
        Path | None,
        typer.Option(
            "--grant-file", help="Scoped danger-zone authorization (see docs/trust-model.md)"
        ),
    ] = None,
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
                grant_file=grant_file,
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
            grant_file=grant_file,
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
    check: Annotated[
        bool,
        typer.Option("--check", help="Exit non-zero unless every PR's verdict is APPROVE"),
    ] = False,
) -> None:
    """Run a read-only review agent over one or more PR diffs."""
    root = project.resolve()

    if all_open and prs:
        _err("pass either PR numbers or --all, not both")
        raise typer.Exit(1)
    if not all_open and not prs:
        _err("specify at least one PR number, or --all")
        raise typer.Exit(1)
    if check and surface != "inline":
        _err("--check is only supported with --surface inline")
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
        # Single-PR inline path is unchanged without --check: no summary, just
        # the review text.
        try:
            text = run_review(root, pr_numbers[0], runtime_name=runtime, post_comment=post)
        except (CommandError, RuntimeError) as exc:
            _err(str(exc))
            raise typer.Exit(1) from exc
        typer.echo(text)
        if not check:
            return
        outcome = ReviewOutcome(pr_numbers[0], prompts.verdict_of(text), text=text)
        typer.echo(format_summary([outcome]))
        raise typer.Exit(0 if outcome.status == "approve" else 1)

    outcomes = run_reviews(
        root, pr_numbers, jobs=jobs, post_comment=post, runtime_name=runtime, log=typer.echo
    )
    for outcome in outcomes:
        if outcome.text:
            typer.echo(f"\n=== PR #{outcome.pr} ===\n{outcome.text}")
    typer.echo("\n" + format_summary(outcomes))
    if check:
        raise typer.Exit(0 if all(o.status == "approve" for o in outcomes) else 1)
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
def evolve(
    lane: Annotated[str, typer.Argument(help="Lane to survey, e.g. spec, triage, implement")],
    project: ProjectOpt = Path("."),
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Gather and print the evidence only; spawns no agent"),
    ] = False,
    window_days: Annotated[
        int, typer.Option("--window-days", help="How many days of history to survey")
    ] = 30,
    min_runs: Annotated[
        int,
        typer.Option("--min-runs", help="Minimum runs in the window before a baseline is shown"),
    ] = 5,
) -> None:
    """Survey a lane's run history and evolve its prompt — or pass --dry-run to
    only see the evidence.

    Without --dry-run: a planner agent diagnoses the survey against four named
    failure modes and either opens a draft, human-merge-only PR against
    `prompts/tasks/<lane>.md`, or states a reasoned no-op. Also runs on a
    weekly scheduled cadence in CI, one lane per pipeline call (#153,
    `.github/workflows/evolve-pipeline.yml`).
    """
    from agent_ops import status
    from agent_ops.workflows.evolve import baseline, gather, render_baseline, render_survey

    # prompts/tasks/evolve.md makes "evolve" itself a valid lane name here
    # (self-targeting). Not a deliberately supported case — it has no CI
    # caller workflow, so `run_evolve` always sees zero runs and no-ops.
    names = sorted(set(prompts.task_names()) | set(status.LANES))
    if lane not in names:
        _err(f"unknown lane {lane!r} — expected one of: {', '.join(names)}")
        raise typer.Exit(1)

    root = project.resolve()

    if not dry_run:
        from agent_ops.workflows.evolve import run_evolve

        try:
            changes = run_evolve(root, lane, window_days=window_days, min_runs=min_runs)
        except (CommandError, RuntimeError) as exc:
            _err(str(exc))
            raise typer.Exit(1) from exc
        typer.echo(f"opened draft PR with {len(changes)} change(s)" if changes else "no-op")
        return

    try:
        rows, notes = gather(root, lane, now=datetime.now(UTC), window_days=window_days)
    except CommandError as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc

    for note in notes:
        typer.echo(f"note: {note}")

    typer.echo(render_survey(rows))

    b = baseline(rows, lane=lane, window_days=window_days)
    if b.runs < min_runs:
        typer.echo()
        typer.echo(
            f"only {b.runs} run(s) for {lane!r} in the last {window_days}d "
            f"(need {min_runs}) — not enough evidence for a baseline"
        )
        return

    typer.echo()
    typer.echo(render_baseline(b))


@app.command()
def distill(project: ProjectOpt = Path(".")) -> None:
    """Prune a managed repo's AGENTS.md: cut spent narration, keep durable knowledge."""
    from agent_ops.workflows.distill import run_distill

    try:
        cuts = run_distill(project.resolve())
    except (CommandError, FileExistsError, FileNotFoundError, RuntimeError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"distilled {len(cuts)} cut(s)" if cuts else "nothing to distill")


@app.command()
def merge(
    pr: Annotated[
        list[int] | None,
        typer.Argument(help="PR number(s) to merge into the working branch"),
    ] = None,
    project: ProjectOpt = Path("."),
    override: Annotated[
        bool, typer.Option("--override", help="Human override: merge despite rule violations")
    ] = False,
    check: Annotated[
        bool,
        typer.Option("--check", help="Report merge-rule violations only; never merge"),
    ] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Merge despite runs in flight elsewhere, without confirming"),
    ] = False,
    batch: Annotated[
        bool,
        typer.Option(
            "--batch",
            help="Update, wait for required checks, then merge each PR in order "
            "(no merge queue here)",
        ),
    ] = False,
    all_clean: Annotated[
        bool,
        typer.Option(
            "--all-clean",
            help="With --batch: batch every open PR with no merge-rule violations",
        ),
    ] = False,
) -> None:
    """Squash-merge a PR into the working branch (staging) if all merge rules pass.

    `--batch <PR>...` (or `--batch --all-clean`) updates, waits for required
    checks, then merges each PR in order instead of one at a time — the
    stand-in for a GitHub merge queue on a repo where none is available: with
    `required_status_checks.strict=true`, every merge marks every other open
    PR BEHIND, so merging a round one `agent merge` at a time re-stales the
    rest on every single merge.
    """
    if check and override:
        _err("--check and --override are mutually exclusive")
        raise typer.Exit(1)
    if check and force:
        _err("--check and --force are mutually exclusive")
        raise typer.Exit(1)
    if check and batch:
        _err("--check and --batch are mutually exclusive")
        raise typer.Exit(1)
    if all_clean and not batch:
        _err("--all-clean requires --batch")
        raise typer.Exit(1)
    if all_clean and pr:
        _err("--all-clean does not take explicit PR numbers")
        raise typer.Exit(1)
    if batch and not all_clean and not pr:
        _err("--batch needs at least one PR number, or --all-clean")
        raise typer.Exit(1)
    if not batch and (not pr or len(pr) != 1):
        _err("specify exactly one PR number, or pass --batch for more than one")
        raise typer.Exit(1)

    root = project.resolve()
    try:
        if check:
            assert pr is not None  # guarded above: exactly one, no --batch
            violations = run_merge_check(root, pr[0])
            raise typer.Exit(0 if not violations else 1)

        confirm = typer.confirm if sys.stdin.isatty() else None

        if batch:
            if all_clean:
                pr_numbers = batch_candidates(root)
                if not pr_numbers:
                    typer.echo("no clean open PRs to batch")
                    raise typer.Exit(0)
            else:
                assert pr is not None  # guarded above
                pr_numbers = pr
            ok = run_merge_batch(root, pr_numbers, override=override, force=force, confirm=confirm)
        else:
            assert pr is not None  # guarded above: exactly one, no --batch
            ok = run_merge(root, pr[0], override=override, force=force, confirm=confirm)
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
def init(
    project: ProjectOpt = Path("."),
    print_labels: Annotated[
        bool,
        typer.Option(
            "--print-labels", help="Print the label `gh` commands instead of applying them"
        ),
    ] = False,
) -> None:
    """Scaffold .agent/config.yaml, AGENTS.md, a CLAUDE.md link, and a claim-on-start hook."""
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

    # Claude Code only, and best-effort: a session started by hand is the one
    # case no agent-ops command runs to claim on its behalf (issue #178), and
    # this is what fills that gap without a separate remembered step.
    hook_dst = claims.seed_claim_hook(root, log=_err)
    if hook_dst is not None:
        typer.echo(f"wrote {hook_dst} (claims the issue on session start)")

    gitignore = root / ".gitignore"
    for marker in _missing_gitignore_markers(root):
        with gitignore.open("a") as fh:
            fh.write(f"\n{marker}\n")
        typer.echo(f"added {marker} to .gitignore")

    # A gate label nobody created is a lane nobody can request: the CI
    # pipelines select on it, and a missing label just selects nothing.
    if print_labels:
        typer.echo("\nlabels the lanes use — run once per repo:")
        _print_label_commands()
        _warn_unserviced_labels(root, labels_synced=False)
        return

    # Git discovers a repo by walking UP from cwd, so `agent init --project
    # ./newthing`, where `newthing` is a plain directory inside an existing
    # checkout, would otherwise resolve `remote_slug` to the ENCLOSING repo's
    # `origin` and sync every label there — an unrequested write to a repo
    # nobody named, by the same route `--repo` pinning exists to close off.
    toplevel = run(["git", "rev-parse", "--show-toplevel"], cwd=root, check=False)
    if toplevel.returncode == 0 and Path(toplevel.stdout.strip()) != root.resolve():
        typer.echo(
            f"\n{root} is not a git repository root — it is inside "
            f"{toplevel.stdout.strip()}'s. Labels were not synced, to avoid writing to "
            "that repo instead; run `agent init` from the repo's own root, or with "
            "--print-labels:"
        )
        _print_label_commands()
        _warn_unserviced_labels(root, labels_synced=False)
        return

    slug = github.remote_slug(root)
    if slug is None:
        typer.echo(
            "\nno `origin` remote — labels were not synced; run once you have one, "
            "or with --print-labels:"
        )
        _print_label_commands()
        _warn_unserviced_labels(root, labels_synced=False)
        return

    try:
        sync = github.sync_labels(root, ONBOARDING_LABELS, repo=slug)
    except CommandError as exc:
        # The files on disk above are correct either way — that's the whole
        # argument for not failing `init` here too. `gh` not being logged in,
        # a network blip, a non-GitHub origin, or `gh` not being installed at
        # all (`utils.run(check=False)` returns a synthetic returncode-127
        # process for that last one, which `sync_labels`' own returncode
        # check turns into this same `CommandError`) all land here; fall
        # back to the same paste-able commands as --print-labels rather than
        # leaving the operator with nothing but an error.
        _err(f"\ncould not sync labels: {exc}")
        typer.echo("labels were not synced — run these once you can:")
        _print_label_commands()
        _warn_unserviced_labels(root, labels_synced=False)
        return

    typer.echo()
    if sync.created:
        typer.echo(f"labels created: {', '.join(sync.created)}")
    if sync.updated:
        typer.echo(f"labels updated: {', '.join(sync.updated)}")
    if sync.failed:
        for name, reason in sync.failed:
            _err(f"label {name} failed: {reason}")
    if not (sync.created or sync.updated or sync.failed):
        typer.echo(f"labels: all {len(sync.unchanged)} already match")
    _warn_unserviced_labels(root, labels_synced=True)


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

    configured_runtimes: dict[str, list[str]] = {}
    if config is not None:
        for role_name in ROLE_NAMES:
            for name in config.effective_runtimes(role_name):
                configured_runtimes.setdefault(name, []).append(role_name)

    # Runtime CLIs answer for themselves through the registry, rather than this
    # command carrying vendor binary names. A missing entry in a configured
    # chain is reported here; whether it makes a role unable to start is decided
    # by _report_roles after considering the rest of that role's chain.
    for name in runtime_names():
        if get_runtime(name).available():
            typer.echo(f"✓ {name}")
        elif name in configured_runtimes:
            roles = ", ".join(dict.fromkeys(configured_runtimes[name]))
            typer.echo(f"! {name} (missing; configured for {roles})")
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
        _report_toolchain(config, project.resolve())
        ok = _report_roles(config) and ok
        for warning in ladder_warnings(config):
            typer.echo(f"! {warning}")

    missing = _missing_gitignore_markers(project.resolve())
    if missing:
        typer.echo(f"! .gitignore missing {', '.join(missing)} — run: agent init")

    _report_claims(project.resolve())
    _report_orphans(project.resolve())
    _report_caller_drift(project.resolve())
    checkout_note = _checkout_drift(PLATFORM_ROOT)
    if checkout_note:
        typer.echo(f"! {checkout_note}")

    raise typer.Exit(0 if ok else 1)


def _report_toolchain(config: ProjectConfig, root: Path) -> None:
    """Report `commands.requires` against the project root. Warnings only, never fatal.

    A real run resolves these inside a fresh worktree *after* `commands.setup`,
    so a name missing here may be perfectly well provisioned there — failing
    `doctor` on that would be wrong. Reporting it is not: it tells whoever is
    onboarding a repo what its runs are going to need on the machine.

    The empty case is the one worth a line. `requires: []` is not "this repo
    needs nothing", it is "nobody has said" — and it leaves the failure this
    check exists for (a gate dying on `<tool>: not found` a full plan+implement
    cycle in, issue #246) exactly as unguarded as before.
    """
    declared = config.commands.requires
    if not declared:
        if any(getattr(config.commands, gate, None) for gate in config.loop.gates):
            typer.echo(
                "! commands.requires is empty — nothing checks that `setup` provisions what "
                "the gates need; a missing binary will abort the run mid-attempt instead of "
                "being caught before planning even starts"
            )
        return
    try:
        missing = missing_requirements(config, root)
    except Exception as exc:  # noqa: BLE001 — doctor reports, never crashes
        typer.echo(f"! could not check commands.requires ({exc})")
        return
    resolved = [name for name in declared if name not in missing]
    if resolved:
        typer.echo(f"✓ requires on PATH: {', '.join(resolved)}")
    if missing:
        typer.echo(
            f"! requires not on PATH here: {', '.join(missing)} — a run resolves these in the "
            f"worktree after `commands.setup` ({config.commands.setup or 'unset'}), so this is "
            "only a warning; if setup does not provide them, that run aborts"
        )


def _report_claims(root: Path) -> None:
    """Report claims that outlived their run, and local work nothing has claimed.

    Never fails `doctor` and never crashes it. Both halves are warnings because
    both are recoverable by hand in one command, and because this check is the
    only thing that makes either failure visible at all: a stale claim is an
    issue no lane will pick up and nobody is told about, which is precisely the
    silent-blocking trade `docs/failure-modes.md` exists to keep honest.

    Two shapes are deliberately *not* reported as problems: a claim with no
    local run (another machine may hold it — see `claims.audit`), and a repo
    with no claims at all, which is the ordinary state and does not need a line.
    """
    if github.remote_slug(root) is None:
        # No shared issue tracker, so nothing claims here in the first place
        # (`claims.claim` no-ops the same way). Reporting "could not check"
        # for every scratch checkout would train people to ignore the line.
        return
    try:
        audit = claims.audit(root)
    except Exception as exc:  # noqa: BLE001 — doctor reports, never crashes
        typer.echo(f"! could not check agent claims ({exc})")
        return
    for stale in audit.stale:
        typer.echo(f"! {stale.detail} — clear it with `agent claim {stale.issue} --release`")
    for issue in audit.unclaimed:
        typer.echo(
            f"! #{issue} has a worktree here but no `{claims.CLAIM_LABEL}` label — the CI "
            f"lane can still start its own run on it; claim it with `agent claim {issue}`"
        )


def _report_orphans(root: Path) -> None:
    """Report `claude`/`codex` processes no live run accounts for (issue #279).

    Best-effort, same shape as `_report_claims`: never fails or crashes
    `doctor`, and a degraded probe (missing `ps`/`lsof`, a `git worktree list`
    failure) is its own warning line rather than being silently read as "no
    orphans" — the whole point of surfacing this is to not let a leaked
    process go unnoticed.

    Orphans mapped to a known `fix/issue-N` worktree ride on `discover_runs`'s
    own `orphan_pids` (every such worktree is already one of its rows), rather
    than a second, separately-assembled `live` here — `discover_runs` is the
    one place that already resolves "live" correctly against every signal
    (spawned sessions, `dispatch`'s pre-exec window, and so on), and
    duplicating that logic is exactly the kind of drift two copies invite. The
    second `find_stray_processes` call below is only for the two buckets
    `discover_runs` has no row to hang off of — `unmapped` and `unknown` — so
    it is passed an empty `live`, which does not affect either: both are
    populated before a process's cwd is ever checked against `live`.
    """
    try:
        worktrees = worktree.list_worktrees(root)
    except Exception as exc:  # noqa: BLE001 — doctor reports, never crashes
        typer.echo(f"! could not check for stray agent processes ({exc})")
        return
    worktree_by_issue: dict[int, Path] = {}
    for wt in worktrees:
        issue = issue_from_branch(wt.branch)
        if issue is not None and wt.path.is_dir():
            worktree_by_issue[issue] = wt.path

    # Deduped: `discover_runs` and the `find_stray_processes` call below both
    # shell out to `ps` on their own, so a single `ps` failure would otherwise
    # print the identical warning line twice.
    seen_warnings: set[str] = set()

    def _log(message: str) -> None:
        if message not in seen_warnings:
            seen_warnings.add(message)
            typer.echo(f"! {message}")

    try:
        mapped, _trustworthy = runs_mod.discover_runs(root, log=_log)
        stray = runs_mod.find_stray_processes(root, {}, worktree_by_issue, _log)
    except Exception as exc:  # noqa: BLE001 — doctor reports, never crashes
        typer.echo(f"! could not determine whether stray agent processes exist ({exc})")
        return

    for r in mapped:
        for pid in r.orphan_pids:
            typer.echo(
                f"! #{r.issue}: stray agent pid {pid} still running in this worktree — not "
                "tracked by any live run"
            )
    for pid, path in stray["unmapped"]:
        typer.echo(f"! stray agent pid {pid} running in {path} — not a registered worktree")
    if stray["unknown"]:
        typer.echo(
            "! could not determine whether stray agent processes exist for "
            f"{len(stray['unknown'])} process(es) — their working directory could not be read"
        )


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
        rendered: list[str] = []
        usable = 0
        for provider in report.providers:
            if provider.error:
                rendered.append(f"{provider.runtime} / INVALID ({provider.error})")
                continue
            ladder = " → ".join(provider.fallbacks) if provider.fallbacks else "none configured"
            suffix = ""
            if provider.runtime not in runtime_names():
                suffix = " [unknown provider]"
            elif not get_runtime(provider.runtime).available():
                suffix = " [CLI missing]"
            else:
                usable += 1
            rendered.append(
                f"{provider.runtime} / {provider.model or 'runtime default'} "
                f"(fallbacks: {ladder}){suffix}"
            )
        typer.echo(f"  {report.name}: {' → '.join(rendered)}")
        if report.error:
            _err(f"✗ {report.name}: {report.error}")
            ok = False
            continue
        if usable == 0:
            _err(f"✗ {report.name}: no configured provider has an available CLI")
            ok = False

    in_use = {provider.runtime for report in reports for provider in report.providers}
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
    pipeline: Annotated[
        bool,
        typer.Option(
            "--pipeline",
            help=(
                "Show what is stuck in each pipeline stage (untriaged, agent-ready, "
                "spec-requested, ...) and the age of the oldest item, per repo"
            ),
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
    from agent_ops.status import fleet_failures, fleet_status, pipeline_coverage, pipeline_status

    chosen = [
        flag
        for flag, on in (
            ("--pipelines", pipelines),
            ("--pipeline", pipeline),
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
        elif pipeline:
            pipeline_status(config)
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
def tui(project: ProjectOpt = Path(".")) -> None:
    """Open the pipeline TUI: pipeline/runs/PRs/waiting-on-you on one screen,
    with dispatch and resume — bare `agent` opens the same thing."""
    from agent_ops.tui import run_tui

    try:
        path, sink_error = run_tui(project.resolve())
    except (ValueError, CommandError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    if sink_error:
        _err(f"! {sink_error}; wrote {path} instead")
    if path is not None:
        typer.echo(str(path))


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
