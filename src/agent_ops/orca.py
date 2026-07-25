from __future__ import annotations

import json
import os
import shutil
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from agent_ops.utils import run

# Card statuses shipped with Orca (repo settings can rename/extend them).
STATUS_TODO = "todo"
STATUS_IN_PROGRESS = "in-progress"
STATUS_IN_REVIEW = "in-review"
STATUS_COMPLETED = "completed"

# `worktree list` is paginated; agent fleets are small, so one generous page
# is enough and keeps the call to a single round trip.
_CARD_LIMIT = 200

# Orca picks up a brand-new external worktree on a periodic rescan, not
# instantly (issue #20) — observed at 13-25s, so the budget here covers ~36s.
# Module level so tests can monkeypatch `orca.time.sleep`.
_INDEX_ATTEMPTS = 12
_INDEX_DELAY_S = 3.0


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


def report(wt_path: Path, *, comment: str | None = None, status: str | None = None) -> bool:
    """Best-effort: reflect pipeline state on the worktree's Orca card.

    Orca resolves external (agent-ops-created) worktrees through the `path:`
    selector even when the repo hides them from the sidebar. The IDE is a
    viewport, never a dependency — no Orca means no-op, and a failed update
    is ignored rather than failing the run.

    Returns whether the card was actually updated, so callers that print a
    summary (`agent status --sync-orca`) can tell a no-op from a write. A card
    created moments ago may not exist yet — see `await_indexed`.
    """
    if (comment is None and status is None) or not available():
        return False
    cmd = [executable(), "worktree", "set", "--worktree", f"path:{wt_path}", "--json"]
    if comment is not None:
        cmd += ["--comment", comment]
    if status is not None:
        cmd += ["--workspace-status", status]
    return run(cmd, check=False).returncode == 0


def await_indexed(paths: Sequence[Path]) -> bool:
    """Block until Orca has a card for every path, or the budget runs out.

    Orca finds worktrees it did not create on a periodic rescan, so a card
    written straight after `git worktree add` would be dropped. One bounded
    wait covers a whole batch — the rescan that finds one new checkout finds
    them all — which is why this is not a per-card retry.
    """
    if not paths or not available():
        return False
    pending = list(paths)
    for attempt in range(_INDEX_ATTEMPTS):
        pending = [path for path in pending if not _indexed(path)]
        if not pending:
            return True
        if attempt < _INDEX_ATTEMPTS - 1:
            time.sleep(_INDEX_DELAY_S)
    return False


def _indexed(path: Path) -> bool:
    return _result(["worktree", "show", "--worktree", f"path:{path}"]) is not None


@dataclass(frozen=True)
class Repo:
    """A repo registered in Orca, and where its main clone lives on disk."""

    id: str
    path: Path
    # "owner/name" derived from the git remote, or None when Orca knows the
    # folder but no GitHub remote — such repos have no lanes to mirror.
    slug: str | None


@dataclass(frozen=True)
class Card:
    """One worktree card as Orca sees it (Orca-created or external)."""

    path: Path
    branch: str  # short name, `refs/heads/` stripped
    comment: str
    workspace_status: str | None


def _result(args: list[str]) -> dict[str, Any] | None:
    """Run a read-only `orca <args> --json` and return its `result` object.

    None on every failure path (Orca down, unknown flag, unparseable output):
    reads exist to populate a viewport, so a broken read degrades to "nothing
    known" rather than taking a command down with it.
    """
    if not available():
        return None
    proc = run([executable(), *args, "--json"], check=False)
    if proc.returncode != 0:
        return None
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    result = payload.get("result") if isinstance(payload, dict) else None
    return result if isinstance(result, dict) else None


def repos() -> list[Repo]:
    """Every repo registered in Orca, keyed back to GitHub by its remote."""
    result = _result(["repo", "list"]) or {}
    entries = result.get("repos")
    if not isinstance(entries, list):
        return []
    found: list[Repo] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("id") or not entry.get("path"):
            continue
        found.append(Repo(str(entry["id"]), Path(str(entry["path"])), _slug(entry)))
    return found


def _slug(entry: dict[str, Any]) -> str | None:
    """Extract `owner/name` from Orca's canonical remote key (`github.com/owner/name`)."""
    identity = entry.get("gitRemoteIdentity")
    key = identity.get("canonicalKey") if isinstance(identity, dict) else None
    if not isinstance(key, str) or "/" not in key:
        return None
    return key.partition("/")[2] or None


def cards(repo_id: str) -> list[Card]:
    """Worktree cards Orca tracks for one repo, including external worktrees."""
    result = _result(["worktree", "list", "--repo", f"id:{repo_id}", "--limit", str(_CARD_LIMIT)])
    entries = (result or {}).get("worktrees")
    if not isinstance(entries, list):
        return []
    found: list[Card] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("path"):
            continue
        branch = str(entry.get("branch") or "").removeprefix("refs/heads/")
        status = entry.get("workspaceStatus")
        found.append(
            Card(
                path=Path(str(entry["path"])),
                branch=branch,
                comment=str(entry.get("comment") or ""),
                workspace_status=str(status) if status else None,
            )
        )
    return found
