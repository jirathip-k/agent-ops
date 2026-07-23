from __future__ import annotations

from agent_ops.utils import PLATFORM_ROOT

TASKS_DIR = PLATFORM_ROOT / "prompts" / "tasks"


def render_task(name: str, **fields: str) -> str:
    """Load prompts/tasks/<name>.md and substitute {placeholders}."""
    template = (TASKS_DIR / f"{name}.md").read_text()
    return template.format(**fields)
