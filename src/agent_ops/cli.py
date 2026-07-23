from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

import typer

from agent_ops import __version__, github, surfaces, worktree
from agent_ops import board as board_mod
from agent_ops.config import PROJECT_CONFIG_REL, load_project_config
from agent_ops.runtimes import get_runtime, runtime_names
from agent_ops.utils import PLATFORM_ROOT, CommandError, run
from agent_ops.workflows import run_implement, run_review
from agent_ops.workflows.implement import make_plan
from agent_ops.workflows.merge import run_merge, run_promote

app = typer.Typer(
    name="agent",
    help="agent-ops: orchestrate agentic SDLC workflows across your repos.",
    no_args_is_help=True,
)
worktree_app = typer.Typer(help="Manage per-task worktrees.", no_args_is_help=True)
app.add_typer(worktree_app, name="worktree")
board_app = typer.Typer(help="Sync issues to the GitHub Projects board.", no_args_is_help=True)
app.add_typer(board_app, name="board")

ProjectOpt = Annotated[
    Path, typer.Option("--project", "-C", help="Project repo root (default: cwd)")
]


def _err(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)


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
        )
    except (CommandError, FileExistsError, RuntimeError, FileNotFoundError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    raise typer.Exit(0 if ok else 1)


@app.command()
def dispatch(
    issue: Annotated[int, typer.Argument(help="GitHub issue number to implement")],
    project: ProjectOpt = Path("."),
    surface: Annotated[str, typer.Option(help="Where to run: auto | herdr | background")] = "auto",
    no_pr: Annotated[bool, typer.Option("--no-pr", help="Skip push + PR creation")] = False,
) -> None:
    """Spawn `agent implement` on a visible surface (Herdr tab, background log, ...)."""
    root = project.resolve()
    command = ["agent", "implement", str(issue), "--project", str(root)]
    if no_pr:
        command.append("--no-pr")
    try:
        where = surfaces.pick(surface).spawn(f"agent-issue-{issue}", command, root)
    except (ValueError, CommandError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"dispatched issue #{issue} → {where}")


@app.command()
def plan(
    issue: Annotated[int, typer.Argument(help="GitHub issue number to plan")],
    project: ProjectOpt = Path("."),
    runtime: Annotated[str | None, typer.Option(help="Override runtime")] = None,
    post: Annotated[bool, typer.Option("--post", help="Post the plan as an issue comment")] = False,
) -> None:
    """Run only the planner role (smart model, read-only) and print the plan."""
    root = project.resolve()
    config = load_project_config(root)
    try:
        issue_data = github.get_issue(issue, cwd=root)
        text = make_plan(config, issue_data, root, runtime_override=runtime)
    except (CommandError, RuntimeError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(text)
    if post:
        github.comment_on_issue(issue, f"## Agent plan\n\n{text}", cwd=root)
        typer.echo(f"posted plan on issue #{issue}")


@app.command()
def review(
    pr: Annotated[int, typer.Argument(help="PR number to review")],
    project: ProjectOpt = Path("."),
    runtime: Annotated[str | None, typer.Option(help="Override runtime")] = None,
    post: Annotated[bool, typer.Option("--post", help="Post the review as a PR comment")] = False,
) -> None:
    """Run a read-only review agent over a PR diff."""
    try:
        text = run_review(project.resolve(), pr, runtime_name=runtime, post_comment=post)
    except (CommandError, RuntimeError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(text)


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

    gitignore = root / ".gitignore"
    for marker in (".worktrees/", ".agent-runs/"):
        if not gitignore.exists() or marker not in gitignore.read_text():
            with gitignore.open("a") as fh:
                fh.write(f"\n{marker}\n")
            typer.echo(f"added {marker} to .gitignore")


@app.command()
def doctor(project: ProjectOpt = Path(".")) -> None:
    """Check that required CLIs are installed and the project config is valid."""
    ok = True
    for tool, required in [("git", True), ("gh", True), ("claude", True), ("codex", False)]:
        found = shutil.which(tool) is not None
        mark = "✓" if found else ("✗" if required else "-")
        typer.echo(f"{mark} {tool}{'' if found else ' (missing)' if required else ' (optional)'}")
        ok = ok and (found or not required)

    try:
        config = load_project_config(project.resolve())
        typer.echo(
            f"✓ config valid (runtime: {config.runtime.name}, gates: "
            f"{', '.join(config.loop.gates)})"
        )
        unset = [g for g in config.loop.gates if not getattr(config.commands, g, None)]
        if unset:
            typer.echo(f"! gates with no command configured (will be skipped): {', '.join(unset)}")
    except Exception as exc:  # noqa: BLE001 — doctor reports, never crashes
        _err(f"✗ config error: {exc}")
        ok = False

    raise typer.Exit(0 if ok else 1)


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
def status() -> None:
    """Fleet overview: open PRs and issue buckets for every registered repo."""
    from agent_ops.status import fleet_status

    try:
        fleet_status(board_mod.load_board_config())
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


@board_app.command("sync")
def board_sync() -> None:
    """Add open issues from every repo in config/board.yml to the Projects board."""
    try:
        config = board_mod.load_board_config()
        total = board_mod.sync(config)
    except (CommandError, FileNotFoundError, ValueError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"synced {total} issue(s) across {len(config.repos)} repo(s)")


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
