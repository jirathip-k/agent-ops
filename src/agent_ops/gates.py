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
    """
    results: list[GateResult] = []
    for name in config.loop.gates:
        command = getattr(config.commands, name, None)
        if not command:
            continue
        proc = run(["sh", "-c", command], cwd=cwd, check=False)
        output = tail(proc.stdout + "\n" + proc.stderr)
        results.append(GateResult(name, command, proc.returncode == 0, output))
    return results


def format_failures(failures: list[GateResult]) -> str:
    blocks = [f"### Gate `{f.name}` FAILED (`{f.command}`)\n```\n{f.output}\n```" for f in failures]
    return "\n\n".join(blocks)
