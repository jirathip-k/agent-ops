"""Per-issue run state, derived from worktrees, feedback files and PRs.

No state file (ADR 0003: state lives in GitHub, plus what's already durable
on disk): every signal here is read fresh each time, so there is nothing to
go stale or to reconcile after a crash.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_ops import github, messages, worktree
from agent_ops.utils import CommandError, run

_BRANCH_RE = re.compile(r"^fix/issue-(\d+)$")
_FEEDBACK_RE = re.compile(r"^issue-(\d+)-feedback\.md$")
# `agent-issue-N-<YYYYMMDD>-<HHMMSS>[a-z].log` is what `BackgroundSurface`
# writes today (issue #92); the bare `agent-issue-N.log` form is what it wrote
# before per-attempt log names existed, and is still matched so an upgrade
# doesn't make an existing project's logs stop counting as run signals.
# Deliberately still only the *dispatch* label: `agent-plan-issue-N`,
# `agent-resume-issue-N` and the review labels have never been run candidates
# here, and widening that is a behaviour change, not a rename.
_LOG_RE = re.compile(r"^agent-issue-(\d+)(?:-\d{8}-\d{6}[a-z]?)?\.log$")
_OUTCOME_RE = re.compile(r"^issue-(\d+)-outcome\.json$")
_ETIME_RE = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$")

# `failed` never comes from the derivation below — a run whose gates failed
# looks exactly like an abandoned one from the outside. It only arrives when
# the run says so itself (`messages`), which is the point of issue #98.
TERMINAL_STATES = ("done", "halted", "stopped", "failed")
_POLL_INTERVAL_S = 15.0
_DEFAULT_TIMEOUT_S = 3600.0
# Consecutive untrustworthy polls `wait_for_runs` tolerates before giving up
# rather than risk reporting a degraded "stopped"/"gone" as real. ~1 minute at
# the default interval: comfortably above the 2-poll dispatch debounce, far
# short of the hour-long default timeout.
_MAX_DEGRADED_POLLS = 4

# One retention story for everything `.agent-runs/` accumulates: outcome
# records and run logs both age out after a week. `.agent-runs/` is a
# recent-activity view — long enough to still have last week's evidence when a
# run is being reconstructed, short enough that a long-lived project's
# directory doesn't grow without bound. The two artifacts that are *live
# state* rather than history are exempt: the feedback file is a halt a human
# has yet to answer, and the spawn record is how to reach the current run.
ARTIFACT_TTL_S = 7 * 24 * 3600.0


@dataclass(frozen=True)
class Run:
    issue: int
    state: str  # running | halted | stopped | done
    detail: str


@dataclass(frozen=True)
class Outcome:
    state: str
    pr_url: str | None
    reason: str | None


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


def _is_free_text_flag(token: str) -> bool:
    """True for any spelling of `--message`, including click's attached forms.

    `ps` gives us the shell's post-quoting argv, so a multi-word message is
    several tokens and the scan would resume inside it. Matching only the
    detached spellings let `--message=retry 3 times 82` through, which returned
    `3` — a phantom row for #3 plus the real run reported `stopped`.
    """
    if token in _FREE_TEXT_FLAGS or token.startswith("--message="):
        return True
    # Attached short form (`-mretry`); `--...` is excluded so long options
    # beginning "-m" aren't caught.
    return token.startswith("-m") and not token.startswith("--") and len(token) > 2


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
        if _is_free_text_flag(token):
            # Conservative on purpose: a single-word `--message=hi 82` also
            # returns None, since the token count can't distinguish it from the
            # multi-word case. Missing a run beats inventing one.
            return None
        if token in _VALUE_FLAGS:
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


def _outcome_detail(outcome: Outcome) -> str:
    """Render an `Outcome` the same way `classify` renders a live open PR."""
    if outcome.pr_url is not None:
        try:
            number = int(outcome.pr_url.rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            return outcome.pr_url
        return f"PR #{number} — {outcome.pr_url}"
    return outcome.reason or "(recorded)"


def classify(
    issue: int,
    *,
    worktree_path: Path | None,
    live: tuple[int, str, str] | None,
    has_feedback: bool,
    pr: dict[str, Any] | None,
    outcome: Outcome | None = None,
) -> Run | None:
    """One issue's state from its signals, in the precedence the issue lays out.

    A live process outranks a halt file: `agent resume` leaves the feedback
    file in place while it runs, so without this order a live resume would
    misreport as still-halted.

    A durable `outcome` record (written by `_finish_run` on the way out, issue
    #87) outranks a feedback file: without this, a stale halt file left behind
    by a run that was never cleanly resumed through `_finish_run` would report
    `halted` forever, even after the outcome it recorded (e.g. a merged PR)
    supersedes it (issue #78).
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
    if outcome is not None:
        return Run(issue, outcome.state, _outcome_detail(outcome))
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


def outcome_path(project_root: Path, issue: int) -> Path:
    """Where a durable "this run finished" record is written on the way out.

    Read by `discover_runs` for issues whose other signals (worktree, feedback
    file, open PR) have since been cleared/consumed — see issue #87: without
    this, a successfully finished run vanishes from `agent runs` instead of
    showing `done`.

    Lives here rather than in `workflows/implement.py` because this module
    already owns the record's schema on the read side (`_load_outcomes`), and
    `implement` is no longer the only writer: an agent spawned into a worktree
    reports through the same record (issue #113).
    """
    return project_root / ".agent-runs" / f"issue-{issue}-outcome.json"


def write_outcome(
    project_root: Path,
    issue: int,
    *,
    state: str,
    pr_url: str | None = None,
    reason: str | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> None:
    """Best-effort durable record of a run's outcome; never crashes a successful run.

    Written via a temp-file-then-`replace` so a crash mid-write never leaves a
    torn/partial JSON file for `_load_outcomes` to trip over. Mirrors
    `messages.record_spawn`'s best-effort philosophy: a failed status write
    must not turn a successful run into a crash.
    """
    path = outcome_path(project_root, issue)
    payload = {
        "state": state,
        "pr_url": pr_url,
        "reason": reason,
        "finished_at": time.time(),
    }
    try:
        path.parent.mkdir(exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload))
        tmp_path.replace(path)
    except OSError as exc:
        log(f"could not write outcome record at {path}: {exc}")


def clear_outcome(
    project_root: Path,
    issue: int,
    *,
    log: Callable[[str], None] = lambda _: None,
) -> None:
    """Drop the previous cycle's outcome record; a new cycle supersedes it.

    `classify` ranks `outcome` above `has_feedback` so that a stale halt file
    can't report `halted` forever after the run it belonged to finished (issue
    #78). That precedence is only sound while the record describes the *latest*
    cycle. Nothing else expires it inside `ARTIFACT_TTL_S`, so a second cycle
    on the same issue — reopened issue, follow-up work — would keep reporting
    the first cycle's `done` no matter what the new one does: a halt needing
    `agent resume` would be invisible, and a gate failure that keeps the
    worktree would read as finished instead of `stopped`. Both are the same
    silent-failure class as #78, pointed the worse way.

    It is also what scopes `agent report --if-unreported` to one cycle: with
    the record cleared at spawn time, "an outcome record exists" means "this
    cycle already reported", which is the whole basis for the stop hook firing
    at most once per spawn (issue #113).

    Best-effort like the rest of this module's status bookkeeping: a run must
    never crash because a stale status file could not be removed. Failing to
    delete it only leaves the pre-existing staleness in place.
    """
    path = outcome_path(project_root, issue)
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log(f"could not clear stale outcome record at {path}: {exc}")


def _load_outcomes(
    run_files: list[Path], now: float, log: Callable[[str], None]
) -> dict[int, Outcome]:
    """Parse every `issue-N-outcome.json`, pruning ones older than `ARTIFACT_TTL_S`.

    Pruning is keyed on the file's own mtime, not the JSON's `finished_at`
    field, so a corrupt file still ages out instead of lingering forever —
    this is a recent-activity view, not an audit log. `prune_logs` ages run
    logs out of the same directory on the same clock.
    """
    outcomes: dict[int, Outcome] = {}
    for path in run_files:
        match = _OUTCOME_RE.match(path.name)
        if match is None:
            continue
        issue = int(match.group(1))
        try:
            age = now - path.stat().st_mtime
        except OSError as exc:
            log(f"warning: could not stat outcome record {path}: {exc}")
            continue
        if age > ARTIFACT_TTL_S:
            path.unlink(missing_ok=True)
            continue
        try:
            data = json.loads(path.read_text())
            outcomes[issue] = Outcome(
                state=data["state"], pr_url=data.get("pr_url"), reason=data.get("reason")
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            log(f"warning: could not read outcome record {path}: {exc}")
    return outcomes


def prune_logs(
    runs_dir: Path,
    *,
    now: float | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> list[Path]:
    """Delete `.agent-runs/` logs older than `ARTIFACT_TTL_S`; return what went.

    Per-attempt log names (issue #92) mean nothing overwrites anything any
    more, so something has to do the bounding instead. This is that something,
    on the same clock and the same mtime criterion `_load_outcomes` prunes
    outcome records with — `.agent-runs/` has one retention rule, not one per
    artifact.

    Called from the writer (`surfaces.BackgroundSurface.spawn`) rather than
    from `discover_runs`, so the directory is bounded even on a project where
    nobody ever runs `agent runs`.

    A log being written to has a fresh mtime, so a live run's log is never
    eligible; and on POSIX, unlinking a file another process holds open leaves
    that process writing happily to the now-unnamed inode either way. Purely
    best-effort: a prune that fails must never take a spawn down with it.
    """
    now = time.time() if now is None else now
    removed: list[Path] = []
    try:
        candidates = sorted(runs_dir.glob("*.log"))
    except OSError as exc:
        log(f"warning: could not list old run logs in {runs_dir}: {exc}")
        return removed
    for path in candidates:
        try:
            if now - path.stat().st_mtime <= ARTIFACT_TTL_S:
                continue
            path.unlink(missing_ok=True)
        except OSError as exc:
            log(f"warning: could not prune old run log {path}: {exc}")
            continue
        removed.append(path)
    return removed


def discover_runs(project_root: Path, log: Callable[[str], None] = print) -> tuple[list[Run], bool]:
    """Every issue with a run signal, newest first, plus whether this poll's
    signals are trustworthy.

    The second element is `False` whenever `git worktree list` or `gh` (PR
    listing) failed this round and had to be degraded to "treat as empty" —
    that shape is indistinguishable from a genuinely stopped/gone run, so a
    caller comparing across polls (`wait_for_runs`) needs to know not to
    trust it as positive evidence of either.

    Outcome records are not part of that judgement: they are read straight off
    the local `.agent-runs/` directory, with no `gh`/`git` call that could
    degrade, so a poll that finds one is as trustworthy as the filesystem.
    """
    trustworthy = True

    worktree_by_issue: dict[int, Path] = {}
    try:
        worktrees = worktree.list_worktrees(project_root)
    except CommandError as exc:
        log(f"warning: could not list worktrees ({exc}); runs may be misreported as stopped")
        worktrees = []
        trustworthy = False
    for wt in worktrees:
        match = _BRANCH_RE.match(wt.branch)
        if match is not None:
            worktree_by_issue[int(match.group(1))] = wt.path

    runs_dir = project_root / ".agent-runs"
    run_files = list(runs_dir.iterdir()) if runs_dir.is_dir() else []
    feedback_issues = _matches(_FEEDBACK_RE, run_files)
    log_issues = _matches(_LOG_RE, run_files)
    outcomes = _load_outcomes(run_files, time.time(), log)

    candidates = set(worktree_by_issue) | feedback_issues | log_issues | set(outcomes)
    if not candidates:
        return [], trustworthy

    live = live_runs(_ps_output(log), project_root)

    try:
        prs = github.open_prs(project_root)
    except CommandError as exc:
        log(f"warning: could not list open PRs ({exc}); runs may be misreported as stopped")
        prs = []
        trustworthy = False
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
            outcome=outcomes.get(issue),
        )
        if found is not None:
            runs.append(found)
    runs.sort(key=lambda r: r.issue, reverse=True)
    return runs, trustworthy


def report_runs(
    project_root: Path, log: Callable[[str], None] = print, issue: int | None = None
) -> None:
    # A one-shot snapshot has no other poll to compare against, and the
    # warning (if any) is already logged by `discover_runs` itself.
    runs, _trustworthy = discover_runs(project_root, log=log)
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


def _observed(msg: messages.RunMessage | None, r: Run | None) -> tuple[str, str]:
    """One watched issue's `(state, detail)` from a report and/or a poll row.

    A report wins over the derivation; with neither, the issue has vanished
    from every signal, which is `gone`.
    """
    if msg is not None:
        return msg.state, msg.detail
    if r is not None:
        return r.state, r.detail
    return "gone", ""


def _wait_out(
    project_root: Path,
    budget_s: float,
    *,
    wake_on: int | None,
    log: Callable[[str], None],
) -> None:
    """Spend `budget_s` before the next poll, waking early if `wake_on` reports.

    The message replaces the *sleep*, not the poll: when nothing arrives this
    returns after exactly the same interval `time.sleep` would have taken, so
    the wait's cadence and its timeout accounting are unchanged whether or not
    a message bus exists.

    `wake_on` is only set when exactly one watched run is still outstanding —
    `orca orchestration check` takes a single terminal handle, so a wait on
    several runs at once keeps the plain sleep and the ordinary poll latency.

    A wait that returns early having found nothing (no Orca, no handle, a
    wedged CLI) must not turn this loop into a spin, so whatever is left of
    the budget is slept out.
    """
    if wake_on is None:
        time.sleep(budget_s)
        return
    started = time.monotonic()
    if messages.wait_for_message(project_root, wake_on, budget_s, log=log):
        return
    remaining = budget_s - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)


def wait_for_runs(
    project_root: Path,
    *,
    issue: int | None = None,
    timeout_s: float | None = _DEFAULT_TIMEOUT_S,
    interval_s: float = _POLL_INTERVAL_S,
    log: Callable[[str], None] = print,
) -> bool:
    """Block until every watched run reaches a terminal state, printing transitions.

    The watch set is fixed on the first *trustworthy* poll: `issue` if given,
    else every issue `discover_runs` finds at that moment. Runs that appear
    later are not added — a caller waits for what existed when it asked. If
    the very first poll comes back untrustworthy and shows no sign of the
    thing being waited on (the named `issue` absent, or nothing at all in
    watch-all mode), that is not treated as "no run found"/"no agent runs
    found" either: it retries on the next poll instead, subject to the same
    `_MAX_DEGRADED_POLLS` bound described below. A watched issue that later
    disappears entirely (worktree removed, PR merged) transitions to `gone`,
    which counts as terminal.

    `stopped` is not trusted on a single observation: `dispatch` leaves the
    same signature (worktree exists, nothing live yet) for the few seconds
    before the child `agent implement` execs, and a `gh`/`git worktree list`
    outage degrades a real `done` to `stopped` too (`discover_runs`'s
    untrustworthy-poll fallback). Both look identical to a genuinely
    abandoned run for one poll. `stopped` only counts as terminal once it has
    held for two consecutive *trustworthy* polls; `done`, `halted` and `gone`
    come from positive evidence and count immediately — except that a watched
    issue missing from an untrustworthy poll's results is never read as
    `gone`: its previous state carries forward unchanged until a trustworthy
    poll confirms it one way or the other, the same "unknown is not evidence
    of death" reasoning `stopped`'s debounce already relies on.

    An untrustworthy poll also resets every watched issue's `stopped`-streak
    to 0, so a `gh`/worktree blip can never itself advance a run toward the
    2-poll `stopped` threshold. If polls come back untrustworthy for more
    than `_MAX_DEGRADED_POLLS` in a row, this raises `CommandError` instead of
    ever reporting `stopped`/`gone` off degraded data — a sustained outage
    should surface as "we don't know", not as a false terminal verdict. Any
    degradation seen during an otherwise-successful wait is summarized in one
    final log line before returning, since the per-poll `warning:` line is
    deduped down to a single, possibly long-scrolled-away occurrence.

    On top of all of that, a run that was dispatched onto a surface with a
    message bus can *report* its own outcome rather than being inferred at
    (issue #98). Such a report wins: it is the run's own first-person account,
    so it overrides the derived state, counts as terminal immediately with no
    `stopped` debounce, and is believed even mid-outage — it is a local read
    with no `gh`/`git` call that could have degraded. `_finish_run` sends only
    once its cleanup is done, so a pushed `done` can never arrive early.

    A message is only ever consulted to *shortcut* a wait, never to justify
    continuing one, so a missed one changes nothing: every iteration still
    derives state from `discover_runs` on the same cadence, with the same
    debounce and the same degraded-poll bounds. There is no state that exists
    only in a message, and therefore nothing to hang on. The one place a
    message adds an answer rather than hurrying one is the named-issue watch
    set: a run that finished and cleaned up before this call started leaves no
    signal to find, and its unread report turns "no run found" into the
    verdict it actually reached.

    Returns True once every watched issue is terminal (including the
    nothing-to-watch case), False if `timeout_s` elapses first. `timeout_s`
    of None waits forever. One poll always happens before the deadline is
    checked, so a run already terminal returns immediately without sleeping.
    Raises CommandError if an explicitly named `issue` has no run at all —
    unlike the no-issue case, there is nothing to wait on, which a caller
    should not mistake for a run finishing.
    """
    last_warning: str | None = None
    deduped_log = _dedup_warnings(log)

    def capture(message: str) -> None:
        # Remember the raw warning even if `_dedup_warnings` goes on to
        # suppress it from the printed output — the degraded-streak error
        # message below still needs the latest one.
        nonlocal last_warning
        if message.startswith("warning:"):
            last_warning = message
        deduped_log(message)

    interval_s = max(1.0, interval_s)
    deadline = None if timeout_s is None else time.monotonic() + timeout_s

    watch: set[int] | None = None
    states: dict[int, str] = {}
    stopped_streak: dict[int, int] = {}
    pushed: dict[int, messages.RunMessage] = {}
    degraded_streak = 0
    degraded_polls = 0
    total_polls = 0

    def is_terminal(i: int) -> bool:
        state = states[i]
        if state == "stopped":
            return stopped_streak.get(i, 0) >= 2
        return state in TERMINAL_STATES or state == "gone"

    def refresh_pushed(targets: set[int]) -> None:
        """Drain any self-reported outcomes for `targets` we haven't seen yet.

        Deduped-logged, not `capture`d: a broken message bus is not a `gh`
        outage, and must not end up quoted as the reason a degraded-poll
        streak gave up.
        """
        pending = {i for i in targets if i not in pushed}
        if pending:
            pushed.update(messages.collect(project_root, pending, log=deduped_log))

    while True:
        polled, trustworthy = discover_runs(project_root, log=capture)
        found = {r.issue: r for r in polled}
        total_polls += 1

        # Before the watch set is established there is only one issue we could
        # possibly be waiting on by name; after it, the whole set.
        if watch is not None:
            refresh_pushed(watch)
        elif issue is not None:
            refresh_pushed({issue})

        if trustworthy:
            degraded_streak = 0
        else:
            degraded_streak += 1
            degraded_polls += 1
            if degraded_streak > _MAX_DEGRADED_POLLS:
                last = last_warning or "no warning text captured"
                raise CommandError(
                    f"gh/worktree data has been unreliable for {degraded_streak} consecutive "
                    f"polls (last: {last}) — refusing to report stopped/gone; check gh "
                    "auth/network and re-run --wait"
                )

        if watch is None:
            if issue is not None:
                # A run that finished and swept its own signals away leaves
                # nothing for `discover_runs` to find, but its report is still
                # sitting unread — that is an answer, not an absence.
                if issue not in found and issue not in pushed:
                    if trustworthy:
                        if degraded_polls:
                            capture(
                                f"note: PR/worktree data was unreliable for {degraded_polls} of "
                                f"{total_polls} polls in this wait"
                            )
                        raise CommandError(f"no run found for #{issue} — nothing to wait on")
                    # A degraded first poll not showing the named issue is not
                    # trustworthy evidence it doesn't exist — the same "unknown
                    # is not evidence of death" reasoning applied to an
                    # already-watched issue's disappearance, just before the
                    # watch set exists to hold state against. Retry rather than
                    # concluding "no run found"; the degraded_streak bound
                    # above still applies if this persists.
                else:
                    watch = {issue}
            else:
                candidate = set(found)
                if not candidate:
                    if trustworthy:
                        capture("no agent runs found")
                        if degraded_polls:
                            capture(
                                f"note: PR/worktree data was unreliable for {degraded_polls} of "
                                f"{total_polls} polls in this wait"
                            )
                        return True
                    # Same reasoning: an empty result from a degraded poll is
                    # not trustworthy evidence there are no runs at all — wait
                    # for a trustworthy poll before concluding that.
                else:
                    watch = candidate
            if watch is not None:
                # Watch-all mode only learns its set here, so this is the first
                # moment those issues can be asked whether they reported.
                refresh_pushed(watch)
                for i in sorted(watch):
                    msg, r = pushed.get(i), found.get(i)
                    state, detail = _observed(msg, r)
                    capture(f"#{i}  {state:<8}  {detail}")
                    states[i] = state
                    # A self-report is never `stopped`, and a degraded poll must
                    # not seed the debounce off an observation it can't vouch for.
                    stopped_streak[i] = (
                        1 if (trustworthy and msg is None and state == "stopped") else 0
                    )
        else:
            for i in sorted(watch):
                msg, r = pushed.get(i), found.get(i)
                if msg is not None:
                    # The run's own account of how it ended. Positive
                    # first-person evidence outranks anything derived, needs no
                    # debounce, and stands even on a degraded poll.
                    state, detail = _observed(msg, r)
                    stopped_streak[i] = 0
                elif r is None and not trustworthy:
                    # An unexplained disappearance during a degraded poll is not
                    # trustworthy evidence the run is actually gone: hold its
                    # state and streak steady and wait for a clean poll.
                    continue
                else:
                    state, detail = _observed(None, r)
                    if not trustworthy:
                        # An unknown observation is not evidence of death: never
                        # let a degraded poll advance the debounce toward terminal.
                        stopped_streak[i] = 0
                    else:
                        stopped_streak[i] = (
                            stopped_streak.get(i, 0) + 1 if state == "stopped" else 0
                        )
                if state != states[i]:
                    line = f"#{i}  {states[i]} → {state:<8}"
                    if detail:
                        line += f"  {detail}"
                    capture(line)
                    states[i] = state

        if watch is not None and all(is_terminal(i) for i in watch):
            if degraded_polls:
                capture(
                    f"note: PR/worktree data was unreliable for {degraded_polls} of "
                    f"{total_polls} polls in this wait"
                )
            return True

        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return False
        outstanding = [i for i in sorted(watch) if not is_terminal(i)] if watch else []
        _wait_out(
            project_root,
            interval_s if remaining is None else min(interval_s, remaining),
            wake_on=outstanding[0] if len(outstanding) == 1 else None,
            log=deduped_log,
        )
