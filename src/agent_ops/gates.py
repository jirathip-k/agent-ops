from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_ops.config import ProjectConfig
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


def format_failures(failures: list[GateResult]) -> str:
    blocks = [f"### Gate `{f.name}` FAILED (`{f.command}`)\n```\n{f.output}\n```" for f in failures]
    return "\n\n".join(blocks)
