"""Per-issue run state, derived from worktrees, feedback files and PRs.

No state file (ADR 0003: state lives in GitHub, plus what's already durable
on disk): every signal here is read fresh each time, so there is nothing to
go stale or to reconcile after a crash.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_ops import github, worktree
from agent_ops.utils import CommandError, run

_BRANCH_RE = re.compile(r"^fix/issue-(\d+)$")
_FEEDBACK_RE = re.compile(r"^issue-(\d+)-feedback\.md$")
_LOG_RE = re.compile(r"^agent-issue-(\d+)\.log$")
_ETIME_RE = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$")


@dataclass(frozen=True)
class Run:
    issue: int
    state: str  # running | halted | stopped | done
    detail: str


def _fmt_elapsed(etime: str) -> str:
    """`ps -eo etime=` (`[[DD-]HH:]MM:SS`) → a short human duration.

    Falls back to the raw string for anything that doesn't match — a
    liveness report is not worth failing over an unparseable field.
    """
    match = _ETIME_RE.fullmatch(etime.strip())
    if match is None:
        return etime
    days_s, hours_s, minutes_s, _ = match.groups()
    days, hours, minutes = int(days_s or 0), int(hours_s or 0), int(minutes_s)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    # Freshly-dispatched runs are the common case for this command; "0m" reads
    # as though nothing has happened.
    return f"{minutes}m" if minutes else "<1m"


def live_runs(ps_output: str, project_root: Path | None = None) -> dict[int, tuple[int, str, str]]:
    """issue number → (pid, etime, verb) for every live `agent implement|resume|dispatch` process.

    Matches when `agent` (or `agent.exe`) is the invoked program itself — either
    the first argv token, or the second when the kernel has expanded the
    `#!.../python` shebang of the `agent` console-script and put the interpreter
    first. Not merely present somewhere in the command line, which would also
    catch something like `grep agent implement 77`.

    The verb and issue number are found by scanning rather than by position,
    since click accepts options interleaved with the issue argument (e.g.
    `agent implement --project /repo 77`, a shape a human typing by hand will
    produce sooner or later).

    `plan`, `spec`, `review`, `groom` and `scout` are deliberately excluded,
    not merely unimplemented:
    - `classify()` maps liveness onto a `fix/issue-N` worktree row, so
      `running` means "that worktree has an owner". Only implement/resume/
      dispatch ever own one; a read-only `plan` or `review` process would
      mask a genuinely dead implement worktree — the same stopped-while-live
      inversion this command exists to prevent, pointed the other way.
    - `review` is keyed by PR number, which would collide with issue keys in
      this function's return type.
    - `spec` and `groom` do create worktrees, but detached ones
      (`worktree.create_detached`), which `worktree.list_worktrees` doesn't
      surface, and they write nothing under `.agent-runs/`, so they produce
      no `_FEEDBACK_RE`/`_LOG_RE` candidate either — a live one is not a
      `discover_runs` candidate in the first place and has no row to report.
    - `plan` has no worktree and `scout` and `groom` take no issue argument,
      so none of the three has an issue number to key this dict on regardless.

    Issue numbers are per-repo and small, so they collide across the repos
    this tool manages. Every dispatched run carries `--project <root>`
    (`cli.py`); when it's present and points elsewhere, the process belongs to
    a different repo and is dropped. A bare invocation with no `--project`
    (e.g. run from the repo's own cwd) still matches, so this only ever
    removes false positives.
    """
    live: dict[int, tuple[int, str, str]] = {}
    for line in ps_output.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid_s, etime, args = parts
        if not pid_s.isdigit():
            continue
        tokens = args.split()
        start = 1 if tokens and Path(tokens[0]).name.startswith("python") else 0
        if len(tokens) < start + 2:
            continue
        program = Path(tokens[start]).name
        if program not in ("agent", "agent.exe"):
            continue
        rest = tokens[start + 1 :]
        verb = rest[0]
        if verb not in _RUN_VERBS:
            continue
        issue_s = _find_issue(rest[1:])
        if issue_s is None:
            continue
        declared = _declared_project(rest)
        # Exclude only when the declared root is confidently parsed and differs.
        # A path containing a space survives `args.split()` as several tokens, so
        # an unparseable value means "unknown", not "elsewhere" — dropping it
        # there would report a live run as stopped, inverting this filter.
        if (
            project_root is not None
            and declared is not None
            and declared.resolve() != project_root.resolve()
        ):
            continue
        live[int(issue_s)] = (int(pid_s), etime, verb)
    return live


# `dispatch` counts as live: it creates the worktree, then retries the Orca
# attach for up to ~4s before the child `agent implement` execs. Excluding it
# reports a healthy in-flight run as `stopped` for those seconds.
#
# `plan`, `spec`, `review`, `groom` and `scout` are deliberately excluded —
# see the `live_runs` docstring for why each one is out.
_RUN_VERBS = ("implement", "resume", "dispatch")

# Flags of implement/resume/dispatch that take a value in the following
# token, so the scan for the issue number must skip both. `=`-forms (e.g.
# `--project=/repo`) carry their own value and don't need this table.
_VALUE_FLAGS = {
    "--project",
    "-C",
    "--runtime",
    "--plan-file",
    "--message-file",
    "--surface",
}

# `--message`/`-m` take free text, which `ps`'s `args` survives as however
# many space-split tokens the message contains — not the single token
# `_VALUE_FLAGS` skips. Handled separately in `_find_issue`: reaching one of
# these before a digit makes everything after it ambiguous.
_FREE_TEXT_FLAGS = {"--message", "-m"}


def _find_issue(tokens: list[str]) -> str | None:
    """The first bare-digit token, skipping option flags and their values.

    An unrecognized `-`-prefixed token is treated as boolean (skip only
    itself) rather than dropping the scan — that direction keeps the digit
    reachable and so fails toward `running`, matching this module's existing
    bias against false negatives.

    `--message`/`-m` is the opposite case: a quoted message arrives here as
    several tokens, not one, so there is no fixed number of tokens to skip.
    If one of them is reached before any bare digit, the rest of the scan is
    unreliable — return None rather than keying the run off a stray digit
    inside the message text.
    """
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.isdigit():
            return token
        if token in _FREE_TEXT_FLAGS:
            return None
        if token.startswith("-") and "=" not in token and token in _VALUE_FLAGS:
            i += 2
        else:
            i += 1
    return None


def _declared_project(rest: list[str]) -> Path | None:
    """The `--project` root in a dispatched argv, or None when it can't be read.

    Accepts every spelling the CLI does: `--project X`, `-C X`, and click's
    `--project=X`. Returns None when the flag is absent, or when its value was
    split across tokens by a space in the path — an unreadable value must not
    be treated as a mismatch.
    """
    for i, token in enumerate(rest):
        if token.startswith("--project="):
            value = token.partition("=")[2]
            return Path(value) if value else None
        if token in ("--project", "-C"):
            following = rest[i + 1 :]
            if not following:
                return None
            candidate = Path(following[0])
            # A spaced path leaves the remainder as separate tokens; only trust
            # the value when it resolves to something that exists.
            return candidate if candidate.is_dir() else None
    return None


def _ps_output(log: Callable[[str], None]) -> str:
    proc = run(["ps", "-ww", "-eo", "pid=,etime=,args="], check=False)
    if proc.returncode != 0:
        log(f"warning: `ps` failed ({proc.stderr.strip() or 'no output'}); liveness unknown")
        return ""
    return proc.stdout


def classify(
    issue: int,
    *,
    worktree_path: Path | None,
    live: tuple[int, str, str] | None,
    has_feedback: bool,
    pr: dict[str, Any] | None,
) -> Run | None:
    """One issue's state from its signals, in the precedence the issue lays out.

    A live process outranks a halt file: `agent resume` leaves the feedback
    file in place while it runs, so without this order a live resume would
    misreport as still-halted.
    """
    if live is not None:
        pid, etime, verb = live
        prefix = f"worktree {worktree_path}, " if worktree_path is not None else ""
        # `dispatch` is the pre-spawn window: the worktree exists but the child
        # implement hasn't execed yet. Say so rather than implying the work is
        # under way.
        detail = f"{prefix}pid {pid}, {_fmt_elapsed(etime)}"
        if verb == "dispatch":
            detail = f"dispatching — {detail}"
        return Run(issue, "running", detail)
    if has_feedback:
        return Run(issue, "halted", f"self-review — resume with `agent resume {issue}`")
    if pr is not None:
        return Run(issue, "done", f"PR #{pr['number']}")
    if worktree_path is not None:
        # Deliberately not "re-dispatch": worktree.create(reuse=True) accepts a
        # pristine checkout, so acting on that advice spawns a second agent into
        # this same worktree.
        return Run(issue, "stopped", "worktree kept, no PR, no feedback — inspect")
    return None


def _matches(pattern: re.Pattern[str], paths: list[Path]) -> set[int]:
    issues = set()
    for path in paths:
        match = pattern.match(path.name)
        if match is not None:
            issues.add(int(match.group(1)))
    return issues


def discover_runs(project_root: Path, log: Callable[[str], None] = print) -> list[Run]:
    """Every issue with a run signal, newest first."""
    worktree_by_issue: dict[int, Path] = {}
    for wt in worktree.list_worktrees(project_root):
        match = _BRANCH_RE.match(wt.branch)
        if match is not None:
            worktree_by_issue[int(match.group(1))] = wt.path

    runs_dir = project_root / ".agent-runs"
    run_files = list(runs_dir.iterdir()) if runs_dir.is_dir() else []
    feedback_issues = _matches(_FEEDBACK_RE, run_files)
    log_issues = _matches(_LOG_RE, run_files)

    candidates = set(worktree_by_issue) | feedback_issues | log_issues
    if not candidates:
        return []

    live = live_runs(_ps_output(log), project_root)

    try:
        prs = github.open_prs(project_root)
    except CommandError as exc:
        log(f"warning: could not list open PRs ({exc}); runs may be misreported as stopped")
        prs = []
    pr_by_issue: dict[int, dict[str, Any]] = {}
    for issue in candidates:
        for pr in prs:
            if github.pr_references_issue(pr, issue):
                pr_by_issue[issue] = pr
                break

    runs: list[Run] = []
    for issue in candidates:
        wt_path = worktree_by_issue.get(issue)
        display = None
        if wt_path is not None:
            try:
                display = wt_path.relative_to(project_root)
            except ValueError:
                display = wt_path
        found = classify(
            issue,
            worktree_path=display,
            live=live.get(issue),
            has_feedback=issue in feedback_issues,
            pr=pr_by_issue.get(issue),
        )
        if found is not None:
            runs.append(found)
    runs.sort(key=lambda r: r.issue, reverse=True)
    return runs


def report_runs(project_root: Path, log: Callable[[str], None] = print) -> None:
    runs = discover_runs(project_root, log=log)
    if not runs:
        log("no agent runs found")
        return
    for r in runs:
        log(f"#{r.issue}  {r.state:<8}  {r.detail}")
