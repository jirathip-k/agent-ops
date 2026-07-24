from __future__ import annotations

import os
import shutil
import sys
from functools import cache
from pathlib import Path

from agent_ops.utils import run

# Card statuses shipped with Orca (repo settings can rename/extend them).
STATUS_IN_PROGRESS = "in-progress"
STATUS_IN_REVIEW = "in-review"


def executable() -> str:
    """The Orca IDE CLI for this session.

    Orca exports ORCA_CLI_COMMAND in managed sessions. Outside those, bare
    `orca` on Linux usually resolves to the GNOME screen reader and would
    start speech — Orca installs itself as `orca-ide` there instead.
    """
    from_env = os.environ.get("ORCA_CLI_COMMAND")
    if from_env:
        return from_env
    return "orca" if sys.platform == "darwin" else "orca-ide"


@cache
def available() -> bool:
    exe = executable()
    if shutil.which(exe) is None:
        return False
    return run([exe, "status", "--json"], check=False).returncode == 0


def report(wt_path: Path, *, comment: str | None = None, status: str | None = None) -> None:
    """Best-effort: reflect pipeline state on the worktree's Orca card.

    Orca resolves external (agent-ops-created) worktrees through the `path:`
    selector even when the repo hides them from the sidebar. The IDE is a
    viewport, never a dependency — no Orca means no-op, and a failed update
    is ignored rather than failing the run.
    """
    if (comment is None and status is None) or not available():
        return
    cmd = [executable(), "worktree", "set", "--worktree", f"path:{wt_path}", "--json"]
    if comment is not None:
        cmd += ["--comment", comment]
    if status is not None:
        cmd += ["--workspace-status", status]
    run(cmd, check=False)
