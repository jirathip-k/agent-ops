from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from agent_ops.utils import PLATFORM_ROOT

TRIAGE_STUB = PLATFORM_ROOT / "stubs" / "managed-repo-triage.yml"
TRIAGE_CALLER_REL = Path(".github") / "workflows" / "triage.yml"

_DRIFT_SECTIONS = ("secrets", "permissions")

# Blanket grants that make every stub key present by definition.
_SATISFIES_ALL = {"secrets": {"inherit"}, "permissions": {"write-all"}}

# A repo can have an unrelated workflow named triage.yml (stale-bot, labeler),
# and a caller file can hold jobs besides the one calling the pipeline. Compare
# only the jobs that actually call it — otherwise an unrelated workflow gets
# told to add App-token secrets, and a second job's `secrets: inherit` masks a
# genuine gap in the caller job itself.
_PIPELINE_REF = "triage-pipeline.yml"


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
    """Keys under `section:` across all jobs, or None when a blanket grant satisfies everything."""
    keys: set[str] = set()
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        value = job.get(section)
        if value is None:
            continue
        if isinstance(value, str):
            if value in _SATISFIES_ALL[section]:
                return None  # `secrets: inherit` / `permissions: write-all`
            # `permissions: read-all` grants no write scope, and the stub's
            # permissions are mostly writes — contribute nothing so the caller
            # reports them, rather than reading as satisfied.
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


def _caller_jobs(jobs: dict[str, object]) -> dict[str, object]:
    """Only the jobs whose `uses:` points at the reusable triage pipeline."""
    return {
        name: job
        for name, job in jobs.items()
        if isinstance(job, dict) and _PIPELINE_REF in str(job.get("uses", ""))
    }


def triage_caller_drift(root: Path) -> TriageDrift | None:
    """Structural drift in root's triage.yml vs. the stub, or None if the repo has no caller."""
    caller_path = root / TRIAGE_CALLER_REL
    if not caller_path.exists():
        return None

    try:
        stub_jobs = _caller_jobs(_load_jobs(TRIAGE_STUB))
        caller_jobs = _caller_jobs(_load_jobs(caller_path))
    except ValueError as exc:
        return TriageDrift([], [], error=str(exc))

    # A triage.yml that never calls the pipeline isn't a caller at all — same
    # answer as having no file, rather than a warning about secrets it has no
    # use for.
    if not caller_jobs:
        return None

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
