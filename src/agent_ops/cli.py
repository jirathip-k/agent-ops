from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

import typer

from agent_ops import __version__, worktree
from agent_ops.config import PROJECT_CONFIG_REL, load_project_config
from agent_ops.runtimes import get_runtime, runtime_names
from agent_ops.utils import PLATFORM_ROOT, CommandError, run
from agent_ops.workflows import run_implement, run_review

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


@app.command()
def implement(
    issue: Annotated[int, typer.Argument(help="GitHub issue number to implement")],
    project: ProjectOpt = Path("."),
    runtime: Annotated[str | None, typer.Option(help="Override runtime")] = None,
    no_pr: Annotated[bool, typer.Option("--no-pr", help="Skip push + PR creation")] = False,
    keep_worktree: Annotated[bool, typer.Option(help="Keep worktree after success")] = False,
) -> None:
    """Implement a GitHub issue: worktree → agent loop → gates → self-review → PR."""
    try:
        ok = run_implement(
            project.resolve(),
            issue,
            runtime_name=runtime,
            open_pr=not no_pr,
            keep_worktree=keep_worktree,
        )
    except (CommandError, FileExistsError, RuntimeError, FileNotFoundError) as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    raise typer.Exit(0 if ok else 1)


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
    marker = ".worktrees/"
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
) -> None:
    config = load_project_config(project.resolve())
    try:
        worktree.remove(project.resolve(), config.worktree_dir, task_id, force=force)
    except CommandError as exc:
        _err(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"removed {task_id}")


if __name__ == "__main__":
    app()
