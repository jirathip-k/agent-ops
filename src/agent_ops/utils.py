from __future__ import annotations

import subprocess
from pathlib import Path

# Repo root of the agent-ops platform itself (src layout: src/agent_ops/utils.py).
# Requires an editable install (`uv tool install --editable .`) so prompts/,
# skills/ and config/ resolve to the checked-out repo.
PLATFORM_ROOT = Path(__file__).resolve().parents[2]


class CommandError(RuntimeError):
    """A subprocess exited non-zero when we required success."""


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise CommandError(
            f"`{' '.join(cmd)}` failed with exit code {proc.returncode}:\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def tail(text: str, lines: int = 40) -> str:
    parts = text.strip().splitlines()
    return "\n".join(parts[-lines:])
