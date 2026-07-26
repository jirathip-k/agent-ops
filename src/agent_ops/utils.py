from __future__ import annotations

import subprocess
from functools import partial
from pathlib import Path

# Repo root of the agent-ops platform itself (src layout: src/agent_ops/utils.py).
# Requires an editable install (`uv tool install --editable .`) so prompts/,
# skills/ and config/ resolve to the checked-out repo.
PLATFORM_ROOT = Path(__file__).resolve().parents[2]

# Default `log` callable for entry points whose output must precede a
# stderr traceback under redirection (e.g. a fallback explanation before the
# error it explains) — plain `print` buffers and can print after it.
flush_print = partial(print, flush=True)


class CommandError(RuntimeError):
    """A subprocess exited non-zero when we required success."""


# Wall-clock bound for the bookkeeping commands that make up almost every
# `run()` call: `gh` API round-trips, `git rev-parse`/`status`/`branch`. These
# finish in seconds, so anything near this bound is a wedged child (a `gh`
# credential prompt on a headless box, a git lock nobody will release), not
# slow-but-real work. Generous enough that a bad network day still succeeds.
DEFAULT_TIMEOUT_S = 120.0

# Git operations whose duration tracks the repo rather than the local index:
# `fetch`/`push` over the network and `worktree add`, which materialises a
# whole working tree. On a large or cold checkout these legitimately run for
# minutes, so they opt out of DEFAULT_TIMEOUT_S rather than raising it for all.
SLOW_GIT_TIMEOUT_S = 600.0


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    check: bool = True,
    timeout: float | None = DEFAULT_TIMEOUT_S,
) -> subprocess.CompletedProcess[str]:
    """Run `cmd` and capture its output.

    `timeout` bounds the wall-clock wait and raises `CommandError` when it
    expires, regardless of `check`: a timeout leaves no exit code and no
    output to inspect, so there is no "failed but usable" result to hand back.
    It defaults to `DEFAULT_TIMEOUT_S` so a wedged child can never strand a run
    — callers that legitimately take longer (project-configured gate commands,
    slow git, an agent CLI) pass their own bound, and `timeout=None` opts out
    entirely.
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandError(f"`{' '.join(cmd)}` did not finish within {timeout:g}s") from exc
    if check and proc.returncode != 0:
        raise CommandError(
            f"`{' '.join(cmd)}` failed with exit code {proc.returncode}:\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def tail(text: str, lines: int = 40) -> str:
    parts = text.strip().splitlines()
    return "\n".join(parts[-lines:])
