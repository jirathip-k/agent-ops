import pytest

from agent_ops.prompts import TASKS_DIR, escalated, render_task

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
        # the #128 regression: a spec that opens by ruling escalation out
        "ESCALATE is not needed — this is a pure UI restyle. Writing the spec.",
        "## Summary\n\nThe planner may ESCALATE: later in prose.",
        "Escalating would be wrong here.",
        "",
    ],
)
def test_escalated_ignores_prose_that_merely_mentions_it(text: str) -> None:
    assert not escalated(text)


def test_prompts_never_instruct_a_bare_escalate_sentinel() -> None:
    """Every prompt must ask for `ESCALATE:` — the check matches nothing less.

    Backticks mark the literal token an agent is told to emit; a prompt that
    says `ESCALATE` without the colon would produce escalations the workflows
    silently accept as success, which is worse than the bug #128 fixed.
    """
    prompts_root = TASKS_DIR.parent
    offenders = [
        path.relative_to(prompts_root).as_posix()
        for path in sorted(prompts_root.rglob("*.md"))
        if "`ESCALATE`" in path.read_text()
    ]
    assert offenders == []
