from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent_ops.utils import PLATFORM_ROOT

TRIAGE_STUB = PLATFORM_ROOT / "stubs" / "managed-repo-triage.yml"
WORKFLOWS_REL = Path(".github") / "workflows"

_DRIFT_SECTIONS = ("secrets", "permissions")

# Blanket grants that make every stub key present by definition.
_SATISFIES_ALL = {"secrets": {"inherit"}, "permissions": {"write-all"}}

# A repo can have an unrelated workflow named triage.yml (stale-bot, labeler),
# and a caller file can hold jobs besides the one calling the pipeline. Compare
# only the jobs that actually call it — otherwise an unrelated workflow gets
# told to add App-token secrets, and a second job's `secrets: inherit` masks a
# genuine gap in the caller job itself.
#
# Detection is content-based for the same reason status.detect_lanes is: caller
# filenames vary per repo, but every caller must `uses:` the reusable pipeline.
# Any owner prefix keeps forks working; `.` is the local form the control repo
# itself would use; `.yaml` is as valid as `.yml`.
_PIPELINE_USES_RE = re.compile(
    r"(?:[\w.-]+/agent-ops|\.)/\.github/workflows/triage-pipeline\.ya?ml(?:@\S+)?"
)


@dataclass(frozen=True)
class TriageDrift:
    """Structural drift between a managed repo's triage.yml and the stub it was copied from."""

    secrets: list[str]
    permissions: list[str]
    error: str | None = None
    path: Path | None = None
    """The caller file this drift was found in, relative to the repo root."""


def _load_workflow(path: Path) -> tuple[dict[str, object], object]:
    """The workflow's `jobs:` mapping and its workflow-level `permissions:` value."""
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
    workflow_permissions = parsed.get("permissions") if isinstance(parsed, dict) else None
    return jobs, workflow_permissions


def _job_section_keys(
    job: dict[str, object], section: str, workflow_value: object
) -> set[str] | None:
    """One job's keys under `section:`, or None when a blanket grant satisfies every key.

    GitHub allows `permissions:` at the workflow root, applying to every job
    that doesn't declare its own — and a job-level block *replaces* it rather
    than merging. Without that fallback, a caller that grants everything at the
    top level reads as granting nothing, and doctor reports all seven
    permissions missing on a correctly-configured repo.
    """
    value = job.get(section)
    if value is None and section == "permissions":
        value = workflow_value
    if value is None:
        return set()
    if isinstance(value, str):
        if value in _SATISFIES_ALL[section]:
            return None  # `secrets: inherit` / `permissions: write-all`
        # `permissions: read-all` grants no write scope, and the stub's
        # permissions are mostly writes — contribute nothing so they're
        # reported, rather than reading as satisfied.
        return set()
    if not isinstance(value, dict):
        # Any other shape (a list, a number) declares no keys we can credit.
        # Reading it as a blanket grant would make the check silently blind,
        # which is the failure this check exists to prevent.
        return set()
    return set(value.keys())


def _ordered_stub_keys(jobs: dict[str, object], section: str, workflow_value: object) -> list[str]:
    ordered: list[str] = []
    for job in jobs.values():
        if not isinstance(job, dict):
            continue
        value = job.get(section)
        if value is None and section == "permissions":
            value = workflow_value
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
        if isinstance(job, dict) and _PIPELINE_USES_RE.search(str(job.get("uses", "")))
    }


def _caller_files(root: Path) -> list[Path]:
    """Every workflow file that calls the triage pipeline, in a stable order.

    Filenames vary per repo — a caller may be `triage.yml`, `agent-triage.yml`,
    or folded into a larger workflow — so the reference to the reusable
    pipeline is the only reliable marker. A cheap text search prefilters before
    the YAML parse.
    """
    workflows = root / WORKFLOWS_REL
    if not workflows.is_dir():
        return []
    found: list[Path] = []
    for path in sorted(workflows.iterdir()):
        if path.suffix not in (".yml", ".yaml") or not path.is_file():
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if _PIPELINE_USES_RE.search(text):
            found.append(path)
    return found


def _drift_in(caller_path: Path, root: Path) -> TriageDrift | None:
    """Drift in one caller file, or None if it turns out not to call the pipeline."""
    rel = caller_path.relative_to(root)
    try:
        stub_all, stub_perms = _load_workflow(TRIAGE_STUB)
        caller_all, caller_perms = _load_workflow(caller_path)
    except ValueError as exc:
        return TriageDrift([], [], error=str(exc), path=rel)

    stub_jobs = _caller_jobs(stub_all)
    caller_jobs = _caller_jobs(caller_all)

    # The text prefilter can match a reference in a comment; the parse is the
    # authority. No caller job means this file isn't a caller after all.
    if not caller_jobs:
        return None

    missing: dict[str, list[str]] = {}
    for section in _DRIFT_SECTIONS:
        stub_keys = _ordered_stub_keys(stub_jobs, section, stub_perms)
        if not stub_keys:
            missing[section] = []
            continue
        # Per job, then union what's *missing* — unioning the keys each job
        # has instead lets one caller job cover for another's gap, which is
        # the exact silent failure this check exists to catch.
        gaps: list[str] = []
        for job in caller_jobs.values():
            if not isinstance(job, dict):
                continue
            job_keys = _job_section_keys(job, section, caller_perms)
            if job_keys is None:
                continue  # blanket grant — this job is fully covered
            gaps.extend(key for key in stub_keys if key not in job_keys and key not in gaps)
        missing[section] = [key for key in stub_keys if key in gaps]

    return TriageDrift(secrets=missing["secrets"], permissions=missing["permissions"], path=rel)


def triage_caller_drift(root: Path) -> TriageDrift | None:
    """Structural drift in the repo's triage-pipeline caller vs. the stub.

    Returns the first caller file that has drift (or an error), so a repo with
    several callers surfaces them one fix at a time rather than merging their
    gaps into one confusing message. None when the repo has no caller at all,
    or every caller is in sync.
    """
    in_sync: TriageDrift | None = None
    for caller_path in _caller_files(root):
        drift = _drift_in(caller_path, root)
        if drift is None:
            continue
        if drift.error or drift.secrets or drift.permissions:
            return drift
        in_sync = in_sync or drift
    return in_sync
