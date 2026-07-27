from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from agent_ops.utils import PLATFORM_ROOT

STUBS_DIR = PLATFORM_ROOT / "stubs"
WORKFLOWS_REL = Path(".github") / "workflows"

# The pairing rule: stubs/managed-repo-<lane>.yml is the caller a managed repo
# copies to call .github/workflows/<lane>-pipeline.yml. That is the only
# lane-specific input the comparison needs, so lanes are discovered from the
# shipped stubs rather than listed here — a seventh stub + pipeline pair is
# covered without touching this module.
_STUB_PREFIX = "managed-repo-"
_STUB_SUFFIXES = (".yml", ".yaml")

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
_PIPELINE_USES = r"(?:[\w.-]+/agent-ops|\.)/\.github/workflows/{lane}-pipeline\.ya?ml(?:@\S+)?"


@dataclass(frozen=True)
class CallerDrift:
    """Structural drift between one lane's caller in a managed repo and its stub."""

    lane: str
    stub: Path
    """The stub the caller was compared against."""
    secrets: list[str]
    permissions: list[str]
    error: str | None = None
    path: Path | None = None
    """The caller file this drift was found in, relative to the repo root."""


def known_lanes() -> list[str]:
    """Every lane the platform ships a stub for, in a stable order.

    Discovered from stubs/ rather than hard-coded so adding a stub + pipeline
    pair extends doctor's coverage on its own.
    """
    if not STUBS_DIR.is_dir():
        return []
    lanes = {
        path.stem.removeprefix(_STUB_PREFIX)
        for path in STUBS_DIR.iterdir()
        if path.is_file() and path.name.startswith(_STUB_PREFIX) and path.suffix in _STUB_SUFFIXES
    }
    return sorted(lanes)


def stub_for(lane: str) -> Path:
    """The stub a lane's caller is compared against.

    Falls back to the `.yml` form when no stub file exists, so a lane whose
    stub has gone missing reports a readable "can't read" error instead of
    silently skipping the check.
    """
    for suffix in _STUB_SUFFIXES:
        candidate = STUBS_DIR / f"{_STUB_PREFIX}{lane}{suffix}"
        if candidate.is_file():
            return candidate
    return STUBS_DIR / f"{_STUB_PREFIX}{lane}.yml"


def _pipeline_pattern(lane: str) -> re.Pattern[str]:
    return re.compile(_PIPELINE_USES.format(lane=re.escape(lane)))


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


def _caller_jobs(jobs: dict[str, object], pattern: re.Pattern[str]) -> dict[str, object]:
    """Only the jobs whose `uses:` points at this lane's reusable pipeline."""
    return {
        name: job
        for name, job in jobs.items()
        if isinstance(job, dict) and pattern.search(str(job.get("uses", "")))
    }


def _caller_files(root: Path, pattern: re.Pattern[str]) -> list[Path]:
    """Every workflow file that calls this lane's pipeline, in a stable order.

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
        if path.suffix not in _STUB_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if pattern.search(text):
            found.append(path)
    return found


def _drift_in(
    caller_path: Path, root: Path, lane: str, pattern: re.Pattern[str]
) -> CallerDrift | None:
    """Drift in one caller file, or None if it turns out not to call the pipeline."""
    rel = caller_path.relative_to(root)
    stub = stub_for(lane)
    try:
        stub_all, stub_perms = _load_workflow(stub)
        caller_all, caller_perms = _load_workflow(caller_path)
    except ValueError as exc:
        return CallerDrift(lane, stub, [], [], error=str(exc), path=rel)

    stub_jobs = _caller_jobs(stub_all, pattern)
    caller_jobs = _caller_jobs(caller_all, pattern)

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

    return CallerDrift(
        lane, stub, secrets=missing["secrets"], permissions=missing["permissions"], path=rel
    )


def caller_workflows(root: Path, lane: str) -> list[Path]:
    """Every workflow file in `root` that calls this lane's reusable pipeline.

    Public wrapper over `_caller_files`, for callers outside the drift check
    that need to point `gh run list --workflow` at the right file (e.g.
    `evolve`, which has no other way to name a lane's CI workflow — caller
    filenames vary per repo).
    """
    return _caller_files(root, _pipeline_pattern(lane))


def lane_caller_drift(root: Path, lane: str) -> CallerDrift | None:
    """Structural drift in the repo's caller for one lane vs. that lane's stub.

    Returns the first caller file that has drift (or an error), so a repo with
    several callers surfaces them one fix at a time rather than merging their
    gaps into one confusing message. None when the repo has no caller for this
    lane at all, or every caller is in sync.
    """
    pattern = _pipeline_pattern(lane)
    in_sync: CallerDrift | None = None
    for caller_path in caller_workflows(root, lane):
        drift = _drift_in(caller_path, root, lane, pattern)
        if drift is None:
            continue
        if drift.error or drift.secrets or drift.permissions:
            return drift
        in_sync = in_sync or drift
    return in_sync


def caller_drift(root: Path) -> list[CallerDrift]:
    """One result per lane the repo actually calls, in lane order.

    Lanes with no caller are left out entirely: a repo that hasn't opted into a
    lane isn't drifting from it, and doctor must not nag it into wiring one up.
    """
    found = (lane_caller_drift(root, lane) for lane in known_lanes())
    return [drift for drift in found if drift is not None]
