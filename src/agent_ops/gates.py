from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from agent_ops.config import PROJECT_CONFIG_REL, ProjectConfig
from agent_ops.utils import run, tail


@dataclass(frozen=True)
class GateResult:
    name: str
    command: str
    ok: bool
    output: str


def run_gates(config: ProjectConfig, cwd: Path) -> list[GateResult]:
    """Run each configured gate (test/lint/typecheck) in order.

    Gates whose command is unset are skipped. All gates run even if an earlier
    one fails, so the retry prompt carries the full picture.

    A gate is arbitrary project-configured shell, so it gets a much longer
    bound than `utils.run`'s default — a real test suite takes minutes. One
    that overruns `loop.gate_timeout_seconds` is reported as a failed gate
    rather than raised: a wedged test is a gate failure like any other, and the
    retry prompt should say so instead of the run dying with a traceback.
    """
    results: list[GateResult] = []
    for name in config.loop.gates:
        command = getattr(config.commands, name, None)
        if not command:
            continue
        proc = run(
            ["sh", "-c", command],
            cwd=cwd,
            check=False,
            timeout=config.loop.gate_timeout_seconds,
        )
        output = tail(proc.stdout + "\n" + proc.stderr)
        results.append(GateResult(name, command, proc.returncode == 0, output))
    return results


def missing_requirements(config: ProjectConfig, cwd: Path) -> list[str]:
    """Which of `commands.requires` do not resolve to an executable in `cwd`.

    Resolution goes through the shell's own `command -v` rather than
    `shutil.which`, and runs in `cwd`, so a name is looked up exactly the way
    the gate commands will look it up: PATH additions the project's `setup`
    made — a `node_modules/.bin` prepended by a wrapper, a venv on the shell
    profile — count here because they count there.

    An empty `requires` returns an empty list without spawning anything, so a
    project that declares nothing pays nothing and behaves as before.
    """
    missing: list[str] = []
    for name in config.commands.requires:
        if not name.strip():
            continue
        proc = run(["sh", "-c", f"command -v {shlex.quote(name)}"], cwd=cwd, check=False)
        if proc.returncode != 0:
            missing.append(name)
    return missing


def format_missing_requirements(
    config: ProjectConfig, missing: list[str], project_root: Path
) -> str:
    """The escalation text for a toolchain gap, addressed to whoever owns the repo.

    Deliberately says which gate commands are at stake and that nothing was
    implemented: the whole point of checking before the planner is that this
    stops reading as "the agent wrote code that fails the tests" (issue #246).
    Which *specific* gate needs which binary is not inferable — `npm run check`
    does not mention Hugo anywhere — so every configured gate is listed and the
    repo is left to make the connection.
    """
    gates_configured = [
        (name, command)
        for name in config.loop.gates
        if (command := getattr(config.commands, name, None))
    ]
    lines = [
        "toolchain preflight failed — these declared binaries are not on PATH in the "
        f"worktree: {', '.join(missing)}",
        "",
        "this is an environment gap, not a code failure: no plan was made, nothing was "
        "implemented, and no gate has run.",
    ]
    if gates_configured:
        lines.append("the gate commands that will need them:")
        lines.extend(f"  {name}: {command}" for name, command in gates_configured)
    setup = config.commands.setup
    lines.append(
        f"`commands.requires` in {project_root / PROJECT_CONFIG_REL} declares what the gates "
        "need on PATH; `commands.setup` "
        + (f"(`{setup}`) is what has to provide it" if setup else "is unset and must provide it")
        + " — mirror whatever this repo's CI does to install these outside its package "
        "manager, then re-dispatch."
    )
    return "\n".join(lines)


def format_failures(failures: list[GateResult]) -> str:
    blocks = [f"### Gate `{f.name}` FAILED (`{f.command}`)\n```\n{f.output}\n```" for f in failures]
    return "\n\n".join(blocks)
