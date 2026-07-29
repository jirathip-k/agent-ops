"""Anti-drift guard for issue #257 (triage lane convergence), the same shape as
`test_review_convergence_drift.py` (#171): a fact with exactly one correct
definition keeps getting reimplemented, slightly differently, by a lane that
didn't know a correct version already existed.

Before this fix, `prompts/orchestrator.md` Step 1 restated the entire
classification contract (buckets, bug priority, stale-claim clearing,
duplicates/questions, the `triage:done` stamp) instead of deferring to
`prompts/tasks/triage.md`, which defined the same three buckets on its own.
This pins that Step 1 now points at the shared prompt rather than repeating
its content, so the two can't drift apart again.
"""

from __future__ import annotations

from agent_ops.utils import PLATFORM_ROOT

TRIAGE_TASK = PLATFORM_ROOT / "prompts" / "tasks" / "triage.md"
ORCHESTRATOR = PLATFORM_ROOT / "prompts" / "orchestrator.md"


def _section(text: str, start_heading: str, end_heading: str) -> str:
    start = text.index(start_heading)
    end = text.index(end_heading, start)
    return text[start:end]


def test_orchestrator_step1_defers_to_the_shared_triage_prompt() -> None:
    step_1 = _section(ORCHESTRATOR.read_text(), "## Step 1", "## Step 2A")

    assert "prompts/tasks/triage.md" in step_1, (
        "Step 1 must apply prompts/tasks/triage.md rather than restate its "
        "own copy of classification -- see issue #257."
    )


def test_bug_priority_definition_lives_only_in_the_shared_prompt() -> None:
    step_1 = _section(ORCHESTRATOR.read_text(), "## Step 1", "## Step 2A")
    task_text = TRIAGE_TASK.read_text()

    assert "production down" in task_text, (
        "prompts/tasks/triage.md must define P0 (production down, data loss, "
        "security exploit) -- this moved from the orchestrator per #257."
    )
    assert "production down" not in step_1, (
        "Step 1 must not restate the P0 definition now that "
        "prompts/tasks/triage.md owns it -- a copy in both places is exactly "
        "the drift #257 exists to remove."
    )


def test_housekeeping_owns_applying_the_bucket_label_and_comment() -> None:
    """Post-#257 review finding: dropping Step 1's old "label + comment"
    action during the restatement removal was exactly the silent
    capability-loss #257 exists to prevent -- applying the classified bucket
    label and posting the `**Triage: <bucket>** -- <reason>` comment is a
    write action, so it belongs in the shared prompt's Housekeeping section
    alongside the other issue-write actions, not left implicit."""
    housekeeping = _section(TRIAGE_TASK.read_text(), "## Housekeeping", "## Defects you discover")

    assert "apply that label" in housekeeping, (
        "prompts/tasks/triage.md's Housekeeping section must instruct "
        "applying the classified bucket label -- not just the terminal-"
        "handling actions (stale-claim, duplicates, questions, triage:done)."
    )
    assert "**Triage: <bucket>**" in housekeeping, (
        "prompts/tasks/triage.md's Housekeeping section must own the exact "
        "comment format a write-capable surface posts alongside the bucket "
        "label."
    )


def test_stale_claim_command_lives_only_in_the_shared_prompt() -> None:
    step_1 = _section(ORCHESTRATOR.read_text(), "## Step 1", "## Step 2A")
    task_text = TRIAGE_TASK.read_text()

    stale_claim_command = "repos/<owner>/<repo>/issues/<N>/events"
    assert stale_claim_command in task_text, (
        "prompts/tasks/triage.md must own the exact `gh api .../events` "
        "command for reading a claim's age -- see issue #257."
    )
    assert stale_claim_command not in step_1, (
        "Step 1 must not restate the stale-claim gh api command now that "
        "prompts/tasks/triage.md owns it."
    )
