import pytest

from agent_ops.prompts import render_task

# task template → the fields the workflow code supplies (a drifted
# placeholder fails render_task with KeyError)
TASK_FIELDS = {
    "plan": {
        "issue_number": "1",
        "issue_title": "t",
        "issue_body": "b",
        "issue_labels": "none",
    },
    "spec": {
        "issue_number": "1",
        "issue_title": "t",
        "issue_body": "b",
        "issue_labels": "none",
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
