import pytest

from agent_ops.prompts import TASKS_DIR, escalated, opens_with_escalation_word, render_task

# task template → the fields the workflow code supplies (a drifted
# placeholder fails render_task with KeyError)
TASK_FIELDS = {
    "plan": {
        "issue_number": "1",
        "issue_title": "t",
        "issue_body": "b",
        "issue_labels": "none",
        "issue_comments": "some comment",
    },
    "spec": {
        "issue_number": "1",
        "issue_title": "t",
        "issue_body": "b",
        "issue_labels": "none",
    },
    "resume": {
        "issue_number": "1",
        "issue_title": "t",
        "issue_body": "b",
        "issue_labels": "none",
        "branch": "fix/issue-1",
        "diff_stat": "1 file changed",
        "feedback": "please add a test",
        "skills": "",
    },
    "scout": {"max_issues": "3"},
    "triage": {"issues": "### #1: t"},
    "groom": {"issues": "### #1: t"},
}


@pytest.mark.parametrize("name", sorted(TASK_FIELDS))
def test_task_templates_render(name: str) -> None:
    # str.format raises on a stray brace or a placeholder the code doesn't fill
    text = render_task(name, **TASK_FIELDS[name])
    for value in TASK_FIELDS[name].values():
        assert value in text


@pytest.mark.parametrize(
    "text",
    [
        "ESCALATE: needs a human",
        "  escalate: lowercase",
        "\n\nESCALATE: after blank lines",
    ],
)
def test_escalated_matches_the_documented_sentinel(text: str) -> None:
    assert escalated(text)


@pytest.mark.parametrize(
    "text",
    [
        # None of these is the documented spelling, and every one of them is a
        # real escalation an agent could plausibly write. Missing one is the
        # silent direction of the bug: the pipeline builds on a task the agent
        # already said it could not do (#129).
        "ESCALATE",
        "ESCALATE\n\nThe schema change needs a product decision first.",
        "ESCALATE — this touches payments",
        "ESCALATE - this touches auth",
        "ESCALATE.",
        "**ESCALATE:** the migration needs sign-off",
        "> ESCALATE: quoted by the agent's own formatting",
        "`ESCALATE:` backticked, as the prompt writes it",
        "## ESCALATE: as a heading",
    ],
)
def test_escalated_catches_undocumented_spellings_of_a_real_escalation(text: str) -> None:
    assert escalated(text)


@pytest.mark.parametrize(
    "text",
    [
        # the #128/#129 regression: a spec that opens by ruling escalation out
        "ESCALATE is not needed — this is a pure UI restyle. Writing the spec.",
        "## Summary\n\nThe planner may ESCALATE: later in prose.",
        "Escalating would be wrong here.",
        "ESCALATED to a human last week, and the answer was to proceed.",
        "",
        "   \n\n",
    ],
)
def test_escalated_ignores_prose_that_merely_mentions_it(text: str) -> None:
    assert not escalated(text)


def test_the_captured_reply_that_broke_the_spec_lane_is_not_an_escalation(
    declined_escalation_reply: str,
) -> None:
    """The exact 2026-07-26 03:48 reply, verbatim from `conftest` (#129)."""
    assert not escalated(declined_escalation_reply)
    # ...and it did match the old rule, so this really is the regression
    assert declined_escalation_reply.lstrip().upper().startswith("ESCALATE")


def test_a_near_miss_is_flagged_so_it_is_never_silent(declined_escalation_reply: str) -> None:
    """Opening with the word is let through — but callers get to say so in the log.

    If an agent did mean to escalate and phrased it that loosely, the near miss
    leaves a trace instead of passing as ordinary output.
    """
    assert opens_with_escalation_word(declined_escalation_reply)
    assert not opens_with_escalation_word("## Summary\n\nAdd a button.")
    assert not opens_with_escalation_word("ESCALATE: a real one is not a near miss")
    assert not opens_with_escalation_word("ESCALATE")


def test_prompts_never_instruct_a_bare_escalate_sentinel() -> None:
    """Every prompt must ask for `ESCALATE:`, the one documented spelling.

    Backticks mark the literal token an agent is told to emit. The check is
    deliberately more forgiving than this (a bare sentinel on its own line
    still counts — see `escalated`), but that tolerance is a safety net for
    output nobody controls, not licence for the prompts to teach a second
    spelling.
    """
    prompts_root = TASKS_DIR.parent
    offenders = [
        path.relative_to(prompts_root).as_posix()
        for path in sorted(prompts_root.rglob("*.md"))
        if "`ESCALATE`" in path.read_text()
    ]
    assert offenders == []
