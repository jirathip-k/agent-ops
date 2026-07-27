from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from agent_ops import github, worktree
from agent_ops.config import load_project_config
from agent_ops.fallback import run_with_fallback
from agent_ops.prompts import render_task
from agent_ops.utils import SLOW_GIT_TIMEOUT_S, CommandError, run
from agent_ops.workflows.implement import role_request

_TASK_ID = "distill"
_BRANCH_PREFIX = "docs/distill"
_AGENTS_FILE = "AGENTS.md"

_HEADING = re.compile(r"^## (.+)$", re.MULTILINE)
_FENCE_LINE = re.compile(r"^```.*$", re.MULTILINE)
_CUT_LINE_SEP = re.compile(r"\s+[—-]+\s+")

# Sentinel key for the pre-heading preamble in `_split_sections`'s return
# value. Never a real heading (no '## ' line produces it), so it can share
# the same before/after comparison as a named protected section without
# risking collision with one.
_PREAMBLE = "\0preamble"


@dataclass(frozen=True)
class DistillCut:
    section: str
    cut: str
    reason: str


def _fenced_ranges(text: str) -> list[tuple[int, int]]:
    """Char spans from each fence-opening ``` line through its matching close.

    An unterminated trailing fence spans to the end of the text rather than
    being dropped, so a stray heading after it is never mistaken for real.
    """
    marks = [m.start() for m in _FENCE_LINE.finditer(text)]
    ranges = []
    for i in range(0, len(marks), 2):
        end = marks[i + 1] if i + 1 < len(marks) else len(text)
        ranges.append((marks[i], end))
    return ranges


def _split_sections(text: str) -> list[tuple[str, str]]:
    """Split `text` on top-level '## ' headings into (heading, body) pairs.

    Content above the first heading (title, intro prose) is returned under
    the `_PREAMBLE` sentinel rather than dropped — `templates/project/AGENTS.md`
    puts real human-authored instruction there, so it must be comparable the
    same way a named protected section is, without being handed to the agent
    as a heading it could rename or prune. A '## ' line inside a fenced code
    block is not a heading — otherwise a protected section that quotes
    Markdown would fork its body at the fence, handing everything after it to
    the agent as an unprotected section.
    """
    fenced = _fenced_ranges(text)
    matches = [m for m in _HEADING.finditer(text) if not any(s <= m.start() < e for s, e in fenced)]
    preamble_end = matches[0].start() if matches else len(text)
    sections = [(_PREAMBLE, text[:preamble_end])]
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections.append((m.group(1).strip(), text[start:end]))
    return sections


def prunable_sections(text: str, protected: Iterable[str]) -> list[tuple[str, str]]:
    """Sections of `text` NOT in `protected` (case- and whitespace-insensitive).

    The preamble sentinel from `_split_sections` never counts as prunable — it
    is not a heading the agent can see or rename, only content that comes
    before the first one.
    """
    protected_norm = {p.strip().lower() for p in protected}
    return [
        (h, b)
        for h, b in _split_sections(text)
        if h != _PREAMBLE and h.lower() not in protected_norm
    ]


def unresolved_protected_sections(text: str, protected: Iterable[str]) -> list[str]:
    """Configured `protected` names with no matching heading in `text`.

    A name that resolves to nothing is a config error (a renamed or deleted
    heading), not "nothing to protect" — treating it as protected-and-absent
    would silently hand its content to the agent as prunable while the guard
    stays quiet, since there is no `after` body to differ from.
    """
    present = {h.lower() for h, _ in _split_sections(text) if h != _PREAMBLE}
    return [name for name in protected if name.strip().lower() not in present]


def protected_sections_changed(before: str, after: str, protected: Iterable[str]) -> list[str]:
    """Protected section names (plus "preamble") whose body differs before/after.

    Empty means every protected section, and the pre-heading preamble, survived
    byte-identical; distillation is subtractive, so this is what stands between
    an agent's edit and a push.
    """
    before_bodies = {h.lower(): b for h, b in _split_sections(before)}
    after_bodies = {h.lower(): b for h, b in _split_sections(after)}
    violated = [
        name
        for name in protected
        if before_bodies.get(name.strip().lower()) != after_bodies.get(name.strip().lower())
    ]
    if before_bodies.get(_PREAMBLE) != after_bodies.get(_PREAMBLE):
        violated.append("preamble")
    return violated


def changed_files_ok(names: list[str]) -> bool:
    """True iff a distill run's diff touches nothing but AGENTS.md (or nothing at all)."""
    return names in ([], [_AGENTS_FILE])


def _changed_paths(cwd: Path) -> list[str]:
    """Every path git currently reports as changed: staged, unstaged, and untracked.

    `-z` NUL-delimits entries so a path containing a space parses correctly,
    and porcelain (not `git diff --name-only`) is what sees untracked files —
    the planner has `Write`, so a relocated file (an archive doc, say) must
    be visible to the "nothing but AGENTS.md" guard too, not just an edit.
    """
    proc = run(["git", "status", "--porcelain", "-z"], cwd=cwd)
    entries = [e for e in proc.stdout.split("\0") if e]
    paths = []
    skip_next = False
    for entry in entries:
        if skip_next:
            skip_next = False  # a rename/copy's old path trails as its own entry
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if status[0] in "RC":
            skip_next = True
    return paths


def parse_distill(text: str) -> list[DistillCut] | None:
    """Parse the DISTILL REPORT block.

    `None` means "no usable report": either the block is missing outright, or
    it is present but nothing in it parsed as a cut or as an explicit `none`
    (a wrapped line, an unexpected dash character). Both must be treated the
    same way by the caller — raise rather than guess — since neither can be
    told apart from a real edit whose report just failed to parse. Only an
    explicit `none` means "definitely no cuts" and returns `[]`.
    """
    _, marker, tail = text.rpartition("DISTILL REPORT:")
    if not marker:
        return None
    cuts = []
    for line in tail.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower() == "none":
            return []
        parts = _CUT_LINE_SEP.split(line, maxsplit=2)
        if len(parts) == 3:
            section, cut, reason = parts
            cuts.append(DistillCut(section.strip(), cut.strip(), reason.strip()))
    return cuts or None


def _distill_branch(project_root: Path, base: str) -> str:
    """A branch name unique to the commit a fresh distill worktree would use.

    A constant branch name means a second run collides with the first run's
    leftover branch, and stays collided for as long as that branch's PR is
    open — naming it after the base commit means a run cut from a later base
    never collides with an older one.
    """
    run(["git", "fetch", "origin", base], cwd=project_root, check=False, timeout=SLOW_GIT_TIMEOUT_S)
    try:
        proc = run(["git", "rev-parse", "--short", f"origin/{base}"], cwd=project_root)
    except CommandError:
        proc = run(["git", "rev-parse", "--short", base], cwd=project_root)
    return f"{_BRANCH_PREFIX}-{proc.stdout.strip()}"


def run_distill(project_root: Path, *, log: Callable[[str], None] = print) -> list[DistillCut]:
    """Prune a managed repo's AGENTS.md: cut spent narration, keep durable knowledge.

    Pre-checks a lean file into a no-op before any agent runs — a file under
    `config.distill.min_lines`, or one where every section is human-authored
    (`config.distill.protected_sections`), costs nothing. An open PR from a
    prior distill run also short-circuits here: a second one stacked on the
    first is not wanted, and the fix is to review or merge the first, not to
    pile up a new run's leftover branch on top of it.

    Before any edit, every configured protected name must resolve to an actual
    heading in the worktree's AGENTS.md — a renamed or deleted heading is a
    config error, not "nothing to protect", and bails rather than silently
    handing that section's content to the agent as prunable.

    Otherwise a planner agent edits AGENTS.md inside a throwaway worktree cut
    from the base branch. Three guards stand between that edit and a push:
    the changed paths — tracked and untracked, since the planner has `Write`
    — must be nothing but AGENTS.md; every protected section's body, and the
    pre-heading preamble, must come back byte-identical; and the DISTILL
    REPORT must actually parse, an unparseable report being treated exactly
    like a missing one rather than like "nothing to distill" — a report is
    the only record of what a real edit removed, and a swallowed one must
    never look like success. Never auto-merges — a docs PR that deletes
    content needs a human who remembers why the content was there.
    """
    config = load_project_config(project_root)
    agents_path = project_root / _AGENTS_FILE
    if not agents_path.is_file():
        log(f"no {_AGENTS_FILE}")
        return []
    text = agents_path.read_text()
    line_count = len(text.splitlines())
    if line_count < config.distill.min_lines:
        log(f"{_AGENTS_FILE} is lean ({line_count} lines)")
        return []
    sections = prunable_sections(text, config.distill.protected_sections)
    if not sections:
        log("every section is protected")
        return []

    try:
        stale_prs = [
            pr
            for pr in github.open_prs(project_root)
            if pr["headRefName"].startswith(_BRANCH_PREFIX)
        ]
    except CommandError:
        stale_prs = []
    if stale_prs:
        pr = stale_prs[0]
        log(
            f"an open distill PR already exists: #{pr['number']} ({pr['url']}) — "
            "review or merge it before running distill again"
        )
        return []

    branch = _distill_branch(project_root, config.base_branch)
    wt_path = worktree.create(
        project_root, config.worktree_dir, _TASK_ID, branch, config.base_branch
    )
    try:
        # The worktree is cut from origin/<base>, which project_root's own
        # checkout may not be — re-read AGENTS.md from inside it so the
        # prompt and the protected-section guard below compare against what
        # the agent actually sees and edits. The pre-checks above stay
        # against the cheap project-root read; a stale local copy only makes
        # them over- or under-cautious about running at all, never unsafe.
        if not (wt_path / _AGENTS_FILE).is_file():
            raise RuntimeError(f"{_AGENTS_FILE} is not on {config.base_branch} — commit it first")
        base_text = (wt_path / _AGENTS_FILE).read_text()
        unresolved = unresolved_protected_sections(base_text, config.distill.protected_sections)
        if unresolved:
            raise RuntimeError(
                f"distill.protected_sections names a heading not in {_AGENTS_FILE}: "
                f"{', '.join(unresolved)}"
            )
        base_sections = prunable_sections(base_text, config.distill.protected_sections)
        prompt = render_task(
            "distill",
            protected_sections="\n".join(f"- {s}" for s in config.distill.protected_sections),
            prunable_sections="\n\n".join(f"## {h}\n{b.strip()}" for h, b in base_sections),
        )
        runtime, request = role_request(
            config,
            "planner",
            prompt,
            wt_path,
            # planner is read-only by default (config.RoleConfig.permission_mode
            # "default") — this is the one lane that needs it to actually edit
            extra_allowed_tools=("Edit", "Write"),
        )
        result = run_with_fallback(runtime, request, on_event=log)
        if not result.ok:
            raise RuntimeError(f"Distill run failed: {result.text}")

        cuts = parse_distill(result.text)
        if cuts is None:
            raise RuntimeError(f"Distill produced no parseable report:\n{result.text[-500:]}")

        changed = _changed_paths(wt_path)
        if not changed_files_ok(changed):
            raise RuntimeError(f"distill touched more than {_AGENTS_FILE}: {changed}")

        if not changed:
            if cuts:
                raise RuntimeError(f"distill reported cuts but {_AGENTS_FILE} is unchanged")
            log("nothing to distill")
            return []
        if not cuts:
            raise RuntimeError(f"distill edited {_AGENTS_FILE} but its report says 'none'")

        new_text = (wt_path / _AGENTS_FILE).read_text()
        violated = protected_sections_changed(
            base_text, new_text, config.distill.protected_sections
        )
        if violated:
            raise RuntimeError(f"distill modified protected section(s): {', '.join(violated)}")

        run(["git", "add", _AGENTS_FILE], cwd=wt_path)
        run(["git", "commit", "-m", f"docs: distill {_AGENTS_FILE}"], cwd=wt_path)
        run(["git", "push", "-u", "origin", branch], cwd=wt_path, timeout=SLOW_GIT_TIMEOUT_S)
        body = "### Pruned\n\n" + "\n".join(
            f"- **{c.section}** — {c.cut} ({c.reason})" for c in cuts
        )
        url = github.create_pr(
            wt_path, base=config.base_branch, title=f"docs: distill {_AGENTS_FILE}", body=body
        )
        log(f"opened PR: {url}")
        for c in cuts:
            log(f"{c.section}: {c.cut}")
        return cuts
    finally:
        worktree.remove(project_root, config.worktree_dir, _TASK_ID, force=True, delete_branch=True)
