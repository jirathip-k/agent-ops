"""Scoped, verifiable danger-zone authorization (#200).

Issue comments are data, never instruction (see prompts/untrusted-data.md) —
so a genuine "yes, touch this danger-zone file" from the repo owner has to
reach an agent through a channel the issue thread cannot forge: the dispatch
invocation itself, via `--grant-file`. A `Grant` is not a boolean; it names
who authorized the change and which paths it covers, in the same glob
vocabulary `MergeConfig.blocked_paths` already enforces at merge time (see
`scope_violations`). See docs/trust-model.md for the full picture.
"""

from __future__ import annotations

from datetime import date
from fnmatch import fnmatch
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from agent_ops.utils import CommandError


class Grant(BaseModel):
    """One issue's scoped authorization to touch otherwise-blocked paths.

    `paths` are `fnmatch` globs, matched case-insensitively exactly like
    `MergeConfig.blocked_paths` — a path a grant doesn't cover is treated as
    ungranted even if it's also blocked, never the reverse.
    """

    issue: int
    granted_by: str
    scope: str
    paths: list[str]
    expires: date | None = None


def load(path: Path, *, issue: int) -> Grant:
    """Parse and validate a grant file, refusing anything that doesn't apply here.

    Raises `CommandError` on anything wrong with the file — unreadable,
    malformed, authorizing a different issue, or expired — rather than
    quietly falling back to "no grant", which would understate what was
    asked for instead of refusing it outright.
    """
    try:
        text = path.read_text()
    except OSError as exc:
        raise CommandError(f"could not read grant file {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise CommandError(f"grant file {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise CommandError(f"grant file {path} must contain a YAML mapping")
    try:
        grant = Grant.model_validate(data)
    except ValidationError as exc:
        raise CommandError(f"grant file {path} is invalid: {exc}") from exc
    if grant.issue != issue:
        raise CommandError(
            f"grant file {path} authorizes issue #{grant.issue}, not #{issue} — refusing to "
            "apply it here"
        )
    if grant.expires is not None and grant.expires < date.today():
        raise CommandError(f"grant file {path} expired on {grant.expires} — needs a fresh one")
    return grant


def persist_path(project_root: Path, issue: int) -> Path:
    """Where a grant carries forward across `agent resume`, mirroring the
    feedback file's convention (`.agent-runs/`, not the worktree)."""
    return project_root / ".agent-runs" / f"issue-{issue}-grant.yaml"


def persist(project_root: Path, grant: Grant) -> None:
    """Write `grant` so a later `agent resume` on the same issue picks it up
    without repeating `--grant-file`. Overwrites any earlier cycle's grant."""
    path = persist_path(project_root, grant.issue)
    path.parent.mkdir(exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(yaml.safe_dump(grant.model_dump(mode="json"), sort_keys=False))
    tmp_path.replace(path)


def load_persisted(project_root: Path, issue: int) -> Grant | None:
    """The grant an earlier cycle on this issue persisted, or None.

    Re-validated exactly like a freshly supplied one (issue match, expiry) —
    a grant that expired between cycles must not silently keep applying.
    """
    path = persist_path(project_root, issue)
    if not path.is_file():
        return None
    return load(path, issue=issue)


def scope_violations(changed_paths: list[str], grant: Grant, blocked_paths: list[str]) -> list[str]:
    """Danger-zone changes `grant` does not cover — empty means the run stayed in scope.

    A path outside `blocked_paths` entirely is never flagged: a grant only
    ever narrows what was already restricted, never adds new restrictions of
    its own.
    """
    violations: list[str] = []
    for changed in changed_paths:
        in_danger_zone = any(fnmatch(changed.lower(), pattern.lower()) for pattern in blocked_paths)
        if not in_danger_zone:
            continue
        covered = any(fnmatch(changed.lower(), pattern.lower()) for pattern in grant.paths)
        if not covered:
            violations.append(changed)
    return violations
