"""Per-issue run state, derived from worktrees, feedback files and PRs.

No state file (ADR 0003: state lives in GitHub, plus what's already durable
on disk): every signal here is read fresh each time, so there is nothing to
go stale or to reconcile after a crash.
"""

from __future__ import annotations

import re
import time
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

TERMINAL_STATES = ("done", "halted", "stopped")
_POLL_INTERVAL_S = 15.0
_DEFAULT_TIMEOUT_S = 3600.0


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
    """issue number → (pid, etime) for every `agent implement|resume <N>` process.

    Matches when `agent` (or `agent.exe`) is the invoked program itself — either
    the first argv token, or the second when the kernel has expanded the
    `#!.../python` shebang of the `agent` console-script and put the interpreter
    first. Not merely present somewhere in the command line, which would also
    catch something like `grep agent implement 77`.

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
        if len(tokens) < start + 3:
            continue
        program, verb, issue_s = (
            Path(tokens[start]).name,
            tokens[start + 1],
            tokens[start + 2],
        )
        if program not in ("agent", "agent.exe") or verb not in _RUN_VERBS:
            continue
        if not issue_s.isdigit():
            continue
        rest = tokens[start + 3 :]
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
_RUN_VERBS = ("implement", "resume", "dispatch")


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


def report_runs(
    project_root: Path, log: Callable[[str], None] = print, issue: int | None = None
) -> None:
    runs = discover_runs(project_root, log=log)
    if issue is not None:
        runs = [r for r in runs if r.issue == issue]
    if not runs:
        log(f"no run found for #{issue}" if issue is not None else "no agent runs found")
        return
    for r in runs:
        log(f"#{r.issue}  {r.state:<8}  {r.detail}")


def _dedup_warnings(log: Callable[[str], None]) -> Callable[[str], None]:
    """Wrap `log` so a warning repeated across polls (e.g. `gh` being down) prints once."""
    seen: set[str] = set()

    def wrapped(message: str) -> None:
        if message.startswith("warning:"):
            if message in seen:
                return
            seen.add(message)
        log(message)

    return wrapped


def wait_for_runs(
    project_root: Path,
    *,
    issue: int | None = None,
    timeout_s: float | None = _DEFAULT_TIMEOUT_S,
    interval_s: float = _POLL_INTERVAL_S,
    log: Callable[[str], None] = print,
) -> bool:
    """Block until every watched run reaches a terminal state, printing transitions.

    The watch set is fixed on the first poll: `issue` if given, else every
    issue `discover_runs` finds at that moment. Runs that appear later are not
    added — a caller waits for what existed when it asked. A watched issue
    that later disappears entirely (worktree removed, PR merged) transitions
    to `gone`, which counts as terminal.

    `stopped` is not trusted on a single observation: `dispatch` leaves the
    same signature (worktree exists, nothing live yet) for the few seconds
    before the child `agent implement` execs, and a `gh` outage degrades a
    real `done` to `stopped` too (`discover_runs`'s `prs = []` fallback). Both
    look identical to a genuinely abandoned run for one poll. `stopped` only
    counts as terminal once it has held for two consecutive polls; `done`,
    `halted` and `gone` come from positive evidence and count immediately.

    Returns True once every watched issue is terminal (including the
    nothing-to-watch case), False if `timeout_s` elapses first. `timeout_s`
    of None waits forever. One poll always happens before the deadline is
    checked, so a run already terminal returns immediately without sleeping.
    Raises CommandError if an explicitly named `issue` has no run at all —
    unlike the no-issue case, there is nothing to wait on, which a caller
    should not mistake for a run finishing.
    """
    log = _dedup_warnings(log)
    interval_s = max(1.0, interval_s)
    deadline = None if timeout_s is None else time.monotonic() + timeout_s

    watch: set[int] | None = None
    states: dict[int, str] = {}
    stopped_streak: dict[int, int] = {}

    def is_terminal(i: int) -> bool:
        state = states[i]
        if state == "stopped":
            return stopped_streak.get(i, 0) >= 2
        return state in TERMINAL_STATES or state == "gone"

    while True:
        found = {r.issue: r for r in discover_runs(project_root, log=log)}

        if watch is None:
            if issue is not None:
                if issue not in found:
                    raise CommandError(f"no run found for #{issue} — nothing to wait on")
                watch = {issue}
            else:
                watch = set(found)
                if not watch:
                    log("no agent runs found")
                    return True
            for i in sorted(watch):
                r = found[i]
                log(f"#{i}  {r.state:<8}  {r.detail}")
                states[i] = r.state
                stopped_streak[i] = 1 if r.state == "stopped" else 0
        else:
            for i in sorted(watch):
                r = found.get(i)
                state = r.state if r is not None else "gone"
                stopped_streak[i] = stopped_streak.get(i, 0) + 1 if state == "stopped" else 0
                if state != states[i]:
                    line = f"#{i}  {states[i]} → {state:<8}"
                    if r is not None:
                        line += f"  {r.detail}"
                    log(line)
                    states[i] = state

        if all(is_terminal(i) for i in watch):
            return True

        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return False
        time.sleep(interval_s if remaining is None else min(interval_s, remaining))
