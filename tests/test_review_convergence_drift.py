"""Anti-drift guard for issue #171 (review lane convergence), alongside
`test_changed_paths_drift.py` (#150/#152): a fact with exactly one correct
implementation keeps getting reimplemented, slightly wrong, by a lane that
didn't know a correct version already existed.

Before this fix, `prompts/agents/reviewer.md` asked for free-text "APPROVE /
REQUEST CHANGES" with no anchored sentinel, and the orchestrator (a model)
interpreted that prose to decide both the Step 2A revision-round trigger and
the Step 3 merge gate -- the same fail-open shape #159 already fixed for the
local lane's `prompts/tasks/review.md`. This pins the anchored `VERDICT:`
line in the role file, and pins that the orchestrator's Step 2A review gate
now shells out to `agent review --check` (the tested verdict parser) instead
of restating APPROVE/REQUEST CHANGES judgment as prose.

Converting Step 2A away from a Reviewer subagent removed the orchestrator's
only other reference to `prompts/agents/reviewer.md`, leaving Step 2B's "run
the same four agents" resolving to nothing for its review step -- the P0
hotfix lane silently lost its reviewer. This also pins that Step 2B names
`REVIEWER (prompts/agents/reviewer.md)` explicitly, so that lane's "same four
agents" phrase has a defined source for APPROVE again.
"""

from __future__ import annotations

from agent_ops.prompts import _VERDICT_RE
from agent_ops.utils import PLATFORM_ROOT

REVIEWER_ROLE = PLATFORM_ROOT / "prompts" / "agents" / "reviewer.md"
ORCHESTRATOR = PLATFORM_ROOT / "prompts" / "orchestrator.md"


def _section(text: str, start_heading: str, end_heading: str) -> str:
    start = text.index(start_heading)
    end = text.index(end_heading, start)
    return text[start:end]


def test_reviewer_role_file_has_an_anchored_verdict_line() -> None:
    text = REVIEWER_ROLE.read_text()

    assert any(_VERDICT_RE.match(line) for line in text.splitlines()), (
        "prompts/agents/reviewer.md must require the anchored `VERDICT: APPROVE` / "
        "`VERDICT: REQUEST CHANGES` line that agent_ops.prompts.verdict_of parses -- "
        "free-text APPROVE/REQUEST CHANGES lets a rejection phrased loosely (or "
        "containing the word 'approve' mid-sentence) read as approval (#171, "
        "same fail-open shape #159 fixed locally)."
    )


def test_orchestrator_review_gate_shells_out_to_agent_review_check() -> None:
    step_2a = _section(ORCHESTRATOR.read_text(), "## Step 2A", "## Step 2B")

    assert "agent review" in step_2a and "--check" in step_2a, (
        "Step 2A's review gate must invoke `agent review --check` rather than "
        "restating APPROVE/REQUEST CHANGES judgment as prose -- see issue #171."
    )
    assert "REVIEWER (prompts/agents/reviewer.md)" not in step_2a, (
        "Step 2A no longer spawns a REVIEWER subagent for the normal lane -- "
        "reviewer.md is still used by the (unconverged) Step 2B hotfix lane."
    )


def test_step3_merge_gate_keys_off_agent_review_check_exit_code() -> None:
    step_3 = _section(ORCHESTRATOR.read_text(), "## Step 3", "## Commit & traceability")

    assert "agent review --check" in step_3, (
        "The merge gate must key off the Step 2A `agent review --check` exit "
        "code, not a separately-interpreted 'Reviewer verdict APPROVE' phrase."
    )


def test_step2b_hotfix_lane_still_names_the_reviewer_role_file() -> None:
    step_2b = _section(ORCHESTRATOR.read_text(), "## Step 2B", "## Step 3")

    assert "REVIEWER (prompts/agents/reviewer.md)" in step_2b, (
        "Step 2B's hotfix lane must explicitly name REVIEWER "
        "(prompts/agents/reviewer.md) in its agent roster -- once Step 2A "
        "stopped spawning a Reviewer subagent (#171), Step 2B's 'run the "
        "same four agents' no longer resolved to anything for its review "
        "step, leaving the P0 lane without a defined source for APPROVE."
    )
