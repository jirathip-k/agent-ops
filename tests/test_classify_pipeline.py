"""Regression tests for .github/workflows/classify-pipeline.yml (#262).

Classification used to share triage-pipeline.yml's concurrency lock and its
four-agent implement step's 55-minute budget, so a long implement run
starved the next classification tick and `untriaged` aged even while that
pipeline was "running". This pipeline runs `agent triage` on its own lock
instead -- these tests pin the YAML shape that makes that true, and execute
the precheck's `jq` program rather than merely asserting its text (a jq
program that merely reads as plausible is exactly what shipped broken in
the abandoned first attempt at this issue).
"""

from __future__ import annotations

import inspect
import json
import re
import shutil

import pytest
import yaml

from agent_ops import claims
from agent_ops.utils import PLATFORM_ROOT, run
from agent_ops.workflows.triage import BUCKET_LABELS, run_triage

_PIPELINE_PATH = PLATFORM_ROOT / ".github" / "workflows" / "classify-pipeline.yml"
_PIPELINE_TEXT = _PIPELINE_PATH.read_text()
_JOB = yaml.safe_load(_PIPELINE_TEXT)["jobs"]["classify"]
_STEPS = _JOB["steps"]
_STEP_NAMES = [step.get("name") for step in _STEPS]


def test_run_step_shells_out_to_agent_triage_with_no_dispatch() -> None:
    run_step = _STEPS[_STEP_NAMES.index("Run classify")]
    assert run_step["run"].strip() == 'uv run agent triage -C "$GITHUB_WORKSPACE/target"'


def test_concurrency_group_is_keyed_by_lane_and_repo() -> None:
    concurrency = _JOB["concurrency"]
    assert concurrency["group"] == "agent-classify-${{ inputs.target_repo }}"
    assert concurrency["cancel-in-progress"] is False


def test_target_checkout_is_unshallow() -> None:
    """run_triage fetches origin/<base_branch> and creates a detached worktree
    from it -- a shallow checkout has no history for that fetch to extend."""
    checkout = _STEPS[_STEP_NAMES.index("Checkout target repo")]
    assert checkout["with"]["fetch-depth"] == 0


def test_permissions_are_exactly_contents_read_and_issues_write() -> None:
    assert _JOB["permissions"] == {"contents": "read", "issues": "write"}


def test_every_step_after_precheck_is_gated_on_it() -> None:
    """Derived from the parsed steps, not a hardcoded name tuple -- a step
    added later without the gate would otherwise run even with 0 candidates."""
    precheck_idx = _STEP_NAMES.index("Check for unclassified issues")
    for step in _STEPS[precheck_idx + 1 :]:
        assert step.get("if") == "steps.precheck.outputs.count != '0'", step.get("name")


def _precheck_run() -> str:
    step = next(s for s in _STEPS if s.get("id") == "precheck")
    return step["run"]


def test_precheck_limit_matches_run_triage_own_fetch_limit() -> None:
    """run_triage fetches --limit 50 issues; a precheck with a lower limit could
    read 0 candidates while run_triage would still find some past that cutoff."""
    precheck_limit = re.search(r"--limit\s+(\d+)", _precheck_run())
    assert precheck_limit is not None
    triage_limit = re.search(r'"--limit",\s*"?(\d+)"', inspect.getsource(run_triage))
    assert triage_limit is not None
    assert precheck_limit.group(1) == triage_limit.group(1)


def _extract_jq_program() -> str:
    match = re.search(r"--jq '(.*?)'\)", _precheck_run(), re.DOTALL)
    assert match is not None, "precheck run: block has no --jq '...' program"
    return match.group(1)


requires_jq = pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed")


def _jq_count(program: str, issues: list[dict[str, object]]) -> int:
    proc = run(["jq", program], input_text=json.dumps(issues))
    return json.loads(proc.stdout)


@requires_jq
def test_precheck_jq_selects_an_unlabeled_issue() -> None:
    assert _jq_count(_extract_jq_program(), [{"labels": []}]) == 1


@requires_jq
def test_precheck_jq_excludes_every_bucket_label() -> None:
    # Derived from BUCKET_LABELS itself: a new bucket added to run_triage
    # without a matching exclusion here would otherwise pass silently.
    program = _extract_jq_program()
    for label in BUCKET_LABELS:
        assert _jq_count(program, [{"labels": [{"name": label}]}]) == 0, label


@requires_jq
def test_precheck_jq_excludes_claimed_issues() -> None:
    program = _extract_jq_program()
    assert _jq_count(program, [{"labels": [{"name": claims.CLAIM_LABEL}]}]) == 0


@requires_jq
def test_precheck_jq_counts_only_matching_issues_in_a_mixed_batch() -> None:
    program = _extract_jq_program()
    issues = [
        {"labels": []},
        {"labels": [{"name": "agent-ready"}]},
        {"labels": [{"name": claims.CLAIM_LABEL}]},
        {"labels": []},
    ]
    assert _jq_count(program, issues) == 2
