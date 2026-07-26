from __future__ import annotations

from agent_ops.utils import PLATFORM_ROOT

TASKS_DIR = PLATFORM_ROOT / "prompts" / "tasks"

#: What the task prompts tell an agent to emit when it needs a human. The colon
#: is part of the sentinel, not punctuation around it.
ESCALATE_SENTINEL = "ESCALATE:"


def render_task(name: str, **fields: str) -> str:
    """Load prompts/tasks/<name>.md and substitute {placeholders}."""
    template = (TASKS_DIR / f"{name}.md").read_text()
    return template.format(**fields)


def escalated(text: str) -> bool:
    """True when agent output opens with the documented escalation sentinel.

    Matches the contract in prompts/tasks/*.md — "a line starting with
    `ESCALATE:`" — no more. Dropping the colon matches prose that merely
    mentions the word, so "ESCALATE is not needed, writing the spec." read as
    an escalation and a finished spec was discarded (#128).
    """
    return text.lstrip().upper().startswith(ESCALATE_SENTINEL)
