from __future__ import annotations

import re

from agent_ops.utils import PLATFORM_ROOT

TASKS_DIR = PLATFORM_ROOT / "prompts" / "tasks"

#: Shared preamble telling every local-lane prompt that issue/PR/CI content is
#: data, not instructions, and what outranks it when the two conflict (#141).
#: Lives in prompts/, not prompts/tasks/, so task_names() doesn't treat it as
#: a lane and evolve's per-lane edit scope doesn't cover it.
UNTRUSTED_DATA_GUARD = (TASKS_DIR.parent / "untrusted-data.md").read_text().strip()

#: What the task prompts tell an agent to emit when it needs a human. The colon
#: is part of the sentinel, not punctuation around it.
ESCALATE_SENTINEL = "ESCALATE:"

#: What `escalated` actually matches on. Deliberately looser than the sentinel
#: the prompts ask for: the colon is what an agent is most likely to drop, and
#: an unrecognized escalation is the silent failure — the run proceeds on a
#: task the agent already said it could not do (#129).
_SENTINEL_WORD = "ESCALATE"

#: What may stand between the word and the reason and still mean escalation:
#: the documented colon, the dashes and terminators agents reach for anyway,
#: or nothing at all (the sentinel alone on its line).
_BOUNDARY_CHARS = frozenset(":-–—.!?,;)]}|/\\")

#: Decoration to see past on either side of the word. Agents echo the prompt's
#: own formatting — `**ESCALATE:**`, `> ESCALATE:`, `## ESCALATE:` — and none
#: of that changes what they meant.
_DECORATION = " \t*_`>#\"'"


def render_task(name: str, **fields: str) -> str:
    """Load prompts/tasks/<name>.md, substitute {placeholders}, and prepend the
    untrusted-data guard. Prepended after `.format()` so guard text never
    itself passes through the template's format string.
    """
    template = (TASKS_DIR / f"{name}.md").read_text()
    return f"{UNTRUSTED_DATA_GUARD}\n\n{template.format(**fields)}"


def task_names() -> list[str]:
    """Every lane with a prompts/tasks/<name>.md template, sorted."""
    return sorted(path.stem for path in TASKS_DIR.glob("*.md"))


def _tail_after_sentinel(text: str) -> str | None:
    """What follows a leading sentinel word on the first non-empty line, or None.

    Decoration and whitespace are stripped from both sides of the word, so
    callers only have to look at the first character of what is left.
    """
    first = next((line for line in text.splitlines() if line.strip()), "")
    line = first.lstrip(_DECORATION)
    if not line.upper().startswith(_SENTINEL_WORD):
        return None
    return line[len(_SENTINEL_WORD) :].lstrip(_DECORATION)


def escalated(text: str) -> bool:
    """True when agent output opens by handing the task back to a human.

    The word only counts with a boundary after it — end of line, the colon
    prompts/tasks/*.md asks for, or other punctuation. A bare prefix match
    also fired on replies that *open with the word* on the way to ruling it
    out ("ESCALATE is not needed — this is a pure UI restyle."), discarding a
    finished spec on every scheduled run (#128).

    Where the two error directions conflict this leans towards catching the
    escalation, because they are not symmetric: a false positive throws away
    one run's work loudly, while a false negative lets a real "I cannot do
    this" read as success and the pipeline builds on it. So `**ESCALATE:**`,
    `ESCALATE — reason` and a bare `ESCALATE` on its own line all count,
    even though the prompts ask for none of those spellings (#129).
    """
    tail = _tail_after_sentinel(text)
    if tail is None:
        return False
    return not tail or tail[0] in _BOUNDARY_CHARS


def opens_with_escalation_word(text: str) -> bool:
    """True for the near-miss shape `escalated` deliberately lets through.

    The first line opens with the word and runs straight on into prose, so it
    is being used as a word rather than as the sentinel. Callers log it: on
    the off chance an agent did mean to escalate and phrased it that loosely,
    the near miss should leave a trace rather than pass in silence.
    """
    tail = _tail_after_sentinel(text)
    return bool(tail) and tail[0] not in _BOUNDARY_CHARS


_VERDICT_RE = re.compile(r"^\s*[`*_]*VERDICT:\s*(APPROVE|REQUEST CHANGES)", re.IGNORECASE)


def verdict_of(text: str) -> str:
    """Read the `VERDICT: ...` line the review prompt requires (see prompts/tasks/review.md).

    Tolerates leading markdown/backtick decoration; an unrecognised or absent
    verdict is `"unknown"`, not a failure — the run still produced a review.
    """
    for line in text.splitlines():
        match = _VERDICT_RE.match(line)
        if match:
            return "approve" if match.group(1).upper() == "APPROVE" else "request_changes"
    return "unknown"
