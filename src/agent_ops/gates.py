from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from agent_ops.config import PROJECT_CONFIG_REL, ProjectConfig, resolved_commands
from agent_ops.utils import SPAWN_FAILURE_RETURNCODE, run, tail

# What a shell says when it couldn't exec the name it was given — dash/sh's
# "sh: 1: <name>: not found" and bash's "<name>: command not found" (with or
# without a leading "bash: line N: "). Anchored on this phrasing, not just the
# returncode, so a script that deliberately `exit 127`s with no such text
# still reads as a real failure rather than an environment gap.
_NOT_FOUND_RE = re.compile(r"([^\s:]+):\s*(?:command\s+)?not found")


class GateStatus(StrEnum):
    """What happened when a gate's command was run."""

    PASSED = "passed"
    FAILED = "failed"
    MISSING = "missing"  # the shell couldn't exec the gate's command at all


class CommandContractLane(StrEnum):
    """How a request may use the resolved executable contract."""

    IMPLEMENTATION = "implementation"
    WORKTREE_READ_ONLY = "worktree-read-only"
    STANDALONE_REVIEW = "standalone-review"


@dataclass(frozen=True)
class GateResult:
    name: str
    command: str
    status: GateStatus
    output: str
    missing_binary: str | None = None

    @property
    def ok(self) -> bool:
        """True only when the gate actually ran and passed.

        Kept so call sites written before `status` existed (`if not g.ok`,
        `g.ok` in an f-string) need no change — `MISSING` is exactly as much
        "not ok" as `FAILED` is, it just isn't retryable the same way.
        """
        return self.status is GateStatus.PASSED


def _names_invoked_command(name: str, command: str) -> bool:
    """Whether `name` (pulled from a "not found" stderr message) is actually a
    token of `command` — i.e. something the gate's own command tried to run —
    rather than unrelated text a script happened to print.

    Allows a path-qualified variant on either side (`/usr/bin/actionlint`
    matching a bare `actionlint`) since that's exactly what the shell vs. the
    gate's own command string might disagree on. A script that deliberately
    `exit 127`s with a message like "widget: not found" has no reason for
    "widget" to coincidentally equal a token of the command that ran it, so
    this closes the false-positive gap while keeping the real case (the gate
    command literally names the missing binary, directly or via a nested
    `bash -c <name>`) working.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    target = Path(name).name
    return any(name == token or target == Path(token).name for token in tokens)


def _classify(
    proc: subprocess.CompletedProcess[str], command: str
) -> tuple[GateStatus, str | None]:
    """Turn a gate's completed process into a `(status, missing_binary)` pair.

    `SPAWN_FAILURE_RETURNCODE` (127) is shell convention for "couldn't exec
    this", but a script can also `exit 127` on purpose with stderr text that
    happens to match the "not found" phrasing for reasons unrelated to PATH
    resolution — that must stay `FAILED`, not be mistaken for the environment
    gap this exists to catch. `_names_invoked_command` disambiguates the two
    by requiring the matched name to actually be a token of `command`.
    `TIMEOUT_RETURNCODE` (124) and every other non-zero code fall straight
    through to `FAILED`.
    """
    if proc.returncode == 0:
        return GateStatus.PASSED, None
    if proc.returncode == SPAWN_FAILURE_RETURNCODE:
        match = _NOT_FOUND_RE.search(proc.stderr)
        if match and _names_invoked_command(match.group(1), command):
            return GateStatus.MISSING, match.group(1)
    return GateStatus.FAILED, None


def run_gates(config: ProjectConfig, cwd: Path) -> list[GateResult]:
    """Run each configured gate (test/lint/typecheck) in order.

    Gates whose command is unset are skipped. All gates run even if an earlier
    one fails, so the retry prompt carries the full picture.

    A gate is arbitrary project-configured shell, so it gets a much longer
    bound than `utils.run`'s default — a real test suite takes minutes. One
    that overruns `loop.gate_timeout_seconds` is reported as a failed gate
    rather than raised: a wedged test is a gate failure like any other, and the
    retry prompt should say so instead of the run dying with a traceback.

    A gate whose binary is not on PATH (`sh -c '<gate>'` exiting 127 with "not
    found" on stderr) is classified `GateStatus.MISSING` rather than `FAILED`
    — see `_classify`. This never raises and never skips later gates, same as
    an ordinary failure; only the caller's retry decision differs (`loop.py`).
    """
    results: list[GateResult] = []
    commands = dict(resolved_commands(config))
    for name in config.loop.gates:
        command = commands.get(name)
        if not command:
            continue
        proc = run(
            ["sh", "-c", command],
            cwd=cwd,
            check=False,
            timeout=config.loop.gate_timeout_seconds,
        )
        output = tail(proc.stdout + "\n" + proc.stderr)
        status, missing_binary = _classify(proc, command)
        results.append(GateResult(name, command, status, output, missing_binary))
    return results


def render_command_contract(config: ProjectConfig, *, lane: CommandContractLane) -> str:
    """Render the resolved setup/gate commands as instructions for agent roles.

    The markers make the contract easy to distinguish from repository
    documentation or untrusted task data. Commands are interpolated verbatim:
    this is the same resolved `ProjectConfig` that setup, `run_gates`,
    requirement checks, and Claude permission patterns consume. Each lane gets
    only the authority that matches the checkout it runs in.
    """
    commands = resolved_commands(config)
    if not commands:
        return "\n".join(
            [
                "<!-- BEGIN AGENT-OPS CONFIGURED EXECUTABLE CONTRACT -->",
                "(nothing is configured; do not invent setup or gate commands)",
                "<!-- END AGENT-OPS CONFIGURED EXECUTABLE CONTRACT -->",
            ]
        )

    lines = [
        "<!-- BEGIN AGENT-OPS CONFIGURED EXECUTABLE CONTRACT -->",
        *(f"{name}: {command}" for name, command in commands),
        "<!-- END AGENT-OPS CONFIGURED EXECUTABLE CONTRACT -->",
        "",
        "These exact strings, resolved from `.agent/config.yaml`, are the sole executable "
        "contract for setup and gates. `AGENTS.md` / `CLAUDE.md` remain authoritative for "
        "repository conventions, safety constraints, and explanations, but not for alternate "
        "command spellings.",
        "",
    ]
    if lane is CommandContractLane.IMPLEMENTATION:
        lines += [
            "Run the configured gates yourself before finishing, exactly as written. For a "
            "targeted test, extend the configured test command with supported arguments; do not "
            "replace its prefix with an underlying runner or a remembered alias.",
            "",
            "If an exact configured command is denied or unavailable, report that command as "
            "UNVERIFIED and name the environment/permission gap. Do not substitute another "
            "command, hand-evaluate tests, or claim the gate passed. Agent-run commands are early "
            "feedback only; the parent `run_gates` execution remains the final pass/fail "
            "authority.",
        ]
    else:
        lines += [
            "These commands are verification context for this read-only lane. Do not run the "
            "configured `setup` command here; setup may modify the checkout and is managed outside "
            "this request. If you run a configured gate for verification, run it exactly as "
            "written. For a targeted test, extend the configured test command with supported "
            "arguments; do not replace its prefix with an underlying runner or a remembered alias.",
            "",
        ]
        if lane is CommandContractLane.STANDALONE_REVIEW:
            lines += [
                "This standalone PR-review checkout does not contain the PR tree under review. "
                "Do not treat gates run in this checkout as verification of the PR diff, and do "
                "not report them as PR gate results.",
                "",
            ]
        lines += [
            "If an exact configured gate command is denied or unavailable, report that command as "
            "UNVERIFIED and name the environment/permission gap. Do not substitute another "
            "command, hand-evaluate tests, or claim the gate passed.",
        ]
    return "\n".join(lines)


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


def _fix_it_note(config: ProjectConfig, project_root: Path) -> str:
    """The closing line both toolchain-gap messages end on: what to fix and where.

    Shared between `format_missing_requirements` (preflight, before anything
    ran) and `format_missing_gate` (mid-run, one gate that never ran) because
    they are the same advice pointed at two different moments — duplicating
    the wording would let the two drift the next time either one is edited.
    """
    setup = config.commands.setup
    return (
        f"`commands.requires` in {project_root / PROJECT_CONFIG_REL} declares what the gates "
        "need on PATH; `commands.setup` "
        + (f"(`{setup}`) is what has to provide it" if setup else "is unset and must provide it")
        + " — mirror whatever this repo's CI does to install these outside its package "
        "manager, then re-dispatch."
    )


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
    resolved = dict(resolved_commands(config))
    gates_configured = [
        (name, command) for name in config.loop.gates if (command := resolved.get(name))
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
    lines.append(_fix_it_note(config, project_root))
    return "\n".join(lines)


def format_missing_gate(config: ProjectConfig, result: GateResult, project_root: Path) -> str:
    """The escalation text for a gate that never ran because its binary is missing.

    Unlike `format_missing_requirements` (a preflight check before anything
    ran), this fires mid-run: `result.status` is `GateStatus.MISSING`, so the
    gate's own command hit `sh: ...: not found` after setup, planning, and at
    least one implementer attempt already happened. Says so plainly, names
    the one gate and its command (the specific connection
    `format_missing_requirements` cannot make — see there), and closes on the
    same `commands.setup` / `commands.requires` fix, so a run that got this
    far still doesn't read as "the implementer's code failed" (issue #287,
    the retryable counterpart to #246).
    """
    binary = result.missing_binary or f"(binary name not extracted; stderr tail: {result.output!r})"
    lines = [
        f"gate `{result.name}` did not run — `{binary}` is not on PATH: `{result.command}`",
        "",
        "this is an environment gap, not a code failure: the gate never produced a real "
        "verdict, so it should not count against the implementer's attempt budget.",
        "",
        _fix_it_note(config, project_root),
    ]
    return "\n".join(lines)


def format_failures(failures: list[GateResult]) -> str:
    blocks = [f"### Gate `{f.name}` FAILED (`{f.command}`)\n```\n{f.output}\n```" for f in failures]
    return "\n\n".join(blocks)
