from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from agent_ops.utils import PLATFORM_ROOT

TRIAGE_STUB = PLATFORM_ROOT / "stubs" / "managed-repo-triage.yml"
TRIAGE_CALLER_REL = Path(".github") / "workflows" / "triage.yml"

_DRIFT_SECTIONS = ("secrets", "permissions")


@dataclass(frozen=True)
class TriageDrift:
    """Structural drift between a managed repo's triage.yml and the stub it was copied from."""

    secrets: list[str]
    permissions: list[str]
    error: str | None = None


def _load_jobs(path: Path) -> dict[str, object]:
    try:
        parsed = yaml.safe_load(path.read_text())
    except OSError as exc:
        # Distinct from a parse failure: "can't parse" reads as malformed YAML
        # and sends you looking at the file's contents instead of its absence.
        raise ValueError(f"can't read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"can't parse {path}: {exc}") from exc
    jobs = parsed.get("jobs") if isinstance(parsed, dict) else None
    if not isinstance(jobs, dict):
        raise ValueError(f"{path} has no jobs: mapping")
    return jobs


def _section_keys(jobs: dict[str, object], section: str) -> set[str] | None:
    """Keys under `section:` across all jobs, or None if any job's value there isn't a mapping."""
    keys: set[str] = set()
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        value = job.get(section)
        if value is None:
            continue
        if not isinstance(value, dict):
            return None
        keys.update(value.keys())
    return keys


def _ordered_stub_keys(jobs: dict[str, object], section: str) -> list[str]:
    ordered: list[str] = []
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        value = job.get(section)
        if not isinstance(value, dict):
            continue
        for key in value:
            if key not in ordered:
                ordered.append(key)
    return ordered


def triage_caller_drift(root: Path) -> TriageDrift | None:
    """Structural drift in root's triage.yml vs. the stub, or None if the repo has no caller."""
    caller_path = root / TRIAGE_CALLER_REL
    if not caller_path.exists():
        return None

    try:
        stub_jobs = _load_jobs(TRIAGE_STUB)
        caller_jobs = _load_jobs(caller_path)
    except ValueError as exc:
        return TriageDrift([], [], error=str(exc))

    missing: dict[str, list[str]] = {}
    for section in _DRIFT_SECTIONS:
        stub_keys = _ordered_stub_keys(stub_jobs, section)
        if not stub_keys:
            missing[section] = []
            continue
        caller_keys = _section_keys(caller_jobs, section)
        if caller_keys is None:
            missing[section] = []
            continue
        missing[section] = [key for key in stub_keys if key not in caller_keys]

    return TriageDrift(secrets=missing["secrets"], permissions=missing["permissions"])
