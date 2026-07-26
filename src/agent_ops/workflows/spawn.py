"""Delegating ad-hoc work to an agent in a worktree, with a completion signal.

`agent dispatch` spawns the *pipeline* — `agent implement`, which reports its
own outcome from inside `_finish_run` (issue #98). This module covers the other
way work gets delegated: a coordinator puts a plain coding-agent session into a
fresh worktree and asks it to do something the pipeline does not model. Until
now that session wrote no spawn record and sent no message, so a supervisor had
nothing to wait on and no way to tell a worker that died from one still working
(issue #113).

The completion signal deliberately does not depend on the agent remembering to
send one. The prompt-instruction workaround ("when you're done, run `orca
orchestration send ...`") fails in exactly the cases worth catching — an agent
that dies, is interrupted, or stops early to escalate never reaches the
instruction. So the report is wired to the *session's* lifecycle instead, via
the runtime's stop hook (`SpawnableRuntime.seed_stop_hook`), which fires whether or not
the agent cooperated.

What that covers, and what it does not:

- finished normally, reported by hand → its own `done` (or `failed`) outcome
- finished normally, said nothing → the hook's `halted`, "stopped without
  reporting"
- stopped early to escalate, went idle waiting for input, was interrupted, or
  exited → the same, via `Stop` or `SessionEnd`
- killed outright (SIGKILL, power loss), or a runtime with no hook mechanism
  (Codex today) → nothing is sent, which is silence: the supervisor's poll
  resolves it exactly as it did before this existed

Nothing here is a state store (ADR 0003). The durable answer is the outcome
record and GitHub; the message is a shortcut that saves a supervisor one poll
interval, and dropping it costs latency, never a run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_ops import messages, orca, runs, surfaces, worktree
from agent_ops.config import load_project_config
from agent_ops.runtimes import get_spawnable_runtime
from agent_ops.workflows.implement import task_identifiers

# The states a worker may report. `done`/`failed` go out as `worker_done`,
# `halted` as an `escalation` — see `messages._TYPE_BY_STATE`. That split is
# what makes "stopped early, needs a human" distinguishable from "finished",
# and both distinguishable from silence.
REPORT_STATES = ("done", "halted", "failed")

# What the stop hook reports for a worker that went quiet without saying
# anything. `halted` on purpose: nobody knows whether the work landed, which is
# a question for a human, not a completion. The reason says all three things it
# could mean, because the hook genuinely cannot tell them apart — `Stop` fires
# whenever the agent finishes responding, which is finished, gave up, or is
# waiting for input.
SILENT_EXIT_STATE = "halted"
SILENT_EXIT_REASON = "stopped without reporting — finished, gave up, or waiting for input"


@dataclass(frozen=True)
class Delegated:
    """One spawned worker: where it runs, and whether it can report back."""

    spawned: surfaces.Spawned
    worktree: Path
    # The file carrying the stop hook, or None when this runtime has no such
    # mechanism — i.e. this worker's completion is inferred, never reported.
    hook_path: Path | None

    @property
    def reports_back(self) -> bool:
        return self.hook_path is not None and self.spawned.handle is not None


def report_command(project_root: Path, issue: int) -> list[str]:
    """Argv the stop hook runs when a spawned session ends.

    `--if-unreported` is what keeps this to one report per spawn: `Stop` fires
    every time the agent finishes responding, and `SessionEnd` fires after it.
    A worker that already reported — by hand, with better detail — silences all
    of them.

    Spelled out rather than hidden behind a terse flag because a human reading
    the seeded settings file should be able to see exactly what will be sent on
    their behalf.
    """
    return [
        "agent",
        "report",
        str(issue),
        "--project",
        str(project_root),
        "--state",
        SILENT_EXIT_STATE,
        "--reason",
        SILENT_EXIT_REASON,
        "--if-unreported",
    ]


def report_outcome(
    project_root: Path,
    issue: int,
    *,
    state: str,
    pr_url: str | None = None,
    reason: str | None = None,
    if_unreported: bool = False,
    log: Callable[[str], None] = lambda _: None,
) -> bool:
    """Record this run's terminal state durably, then push it. Never raises.

    Durable first: the outcome record is what `agent runs` reads and what
    `--if-unreported` keys off, so it must exist before anything can be woken
    by the message. The push is best-effort on top — `send_outcome` already
    returns False for no Orca, no handle, or a failed send.

    Returns whether a message actually went out. Callers should not read that
    as success: the record is written either way, and a supervisor with no
    message still reaches the same verdict by polling.
    """
    if if_unreported and runs.outcome_path(project_root, issue).is_file():
        log(f"#{issue} has already reported this cycle — nothing to send")
        return False
    runs.write_outcome(project_root, issue, state=state, pr_url=pr_url, reason=reason, log=log)
    pushed = messages.send_outcome(
        project_root, issue, state=state, pr_url=pr_url, reason=reason, log=log
    )
    summary = pr_url or reason or "no further detail"
    log(f"#{issue} recorded {state} — {summary}" + ("" if pushed else " (no push channel)"))
    return pushed


def opening_brief(project_root: Path, issue: int, prompt: str | None) -> str:
    """The worker's first message: the brief, plus how to report a better outcome.

    The reporting paragraph is an *upgrade path*, not the mechanism — the hook
    reports regardless. Saying so plainly matters: an agent told that reporting
    is mandatory will burn a turn on it, and one told nothing will leave the
    supervisor with a bare `halted` when it could have said "PR #123".
    """
    brief = prompt or (
        f"Work on GitHub issue #{issue} in this worktree. Read it first "
        f"(`gh issue view {issue}`), then implement it."
    )
    return (
        f"{brief}\n\n"
        f"---\n"
        f"When you finish, report the outcome so whoever delegated this stops waiting:\n"
        f"  agent report {issue} --project {project_root} --state done --pr <pr-url>\n"
        f"If you stop early, say why instead:\n"
        f"  agent report {issue} --project {project_root} --state halted --reason '<blocker>'\n"
        f"This is optional — a stop hook reports a bare {SILENT_EXIT_STATE!r} for you if you "
        f"don't. Running it yourself is what turns that into a useful answer."
    )


def run_spawn(
    project_root: Path,
    issue: int,
    *,
    prompt: str | None = None,
    surface_name: str = "auto",
    runtime_name: str | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> Delegated:
    """Put a coding-agent session in a fresh worktree for `issue`, wired to report.

    The order is load-bearing. The worktree is created here (rather than by
    `orca worktree create --agent`, which would create the checkout and launch
    the agent in one step) because the stop hook has to be on disk *before* the
    CLI starts: Claude Code snapshots its hooks at session startup, so a hook
    seeded a moment later is read by nobody. Creating the checkout first, then
    attaching a terminal to it, is the only ordering in which the hook is ever
    live.

    Everything Orca-shaped stays optional. Without Orca, `surfaces.pick` falls
    through to the background surface, `record_spawn` writes a record with no
    handle, and `send_outcome` no-ops — the worker runs, and the supervisor
    polls, which is what it did before any of this existed.
    """
    config = load_project_config(project_root)
    role = config.resolve_role("implementer", runtime_override=runtime_name)
    runtime = get_spawnable_runtime(role.runtime)
    if not runtime.available():
        raise RuntimeError(f"Runtime {runtime.name!r} CLI is not installed/on PATH")
    chosen = surfaces.pick(surface_name)

    task_id, branch = task_identifiers(issue)
    wt_path = worktree.create(
        project_root, config.worktree_dir, task_id, branch, config.base_branch, reuse=True
    )
    hook_path = runtime.seed_stop_hook(wt_path, report_command(project_root, issue))
    if hook_path is None:
        log(
            f"! {runtime.name} has no stop hook — #{issue} will not report on its own; "
            f"`agent runs {issue}` still derives its state by polling"
        )
    # A new cycle supersedes the last one's verdict, and `--if-unreported` is
    # only meaningful once this cycle's slate is clean.
    runs.clear_outcome(project_root, issue, log=log)

    if isinstance(chosen, surfaces.OrcaSurface):
        # Orca finds an externally-created worktree on a periodic rescan, and
        # the terminal must land on *this* worktree's card: unlike `dispatch`,
        # there is no project-root fallback to take, because the session's
        # shell has to start in the worktree for the agent to work in it.
        orca.await_indexed([wt_path])

    command = runtime.interactive_command(
        opening_brief(project_root, issue, prompt), model=role.model
    )
    spawned = chosen.spawn(f"agent-spawn-issue-{issue}", command, wt_path, attach_path=wt_path)
    # How to reach the worker, written from the handle the surface just
    # returned — the link that was missing entirely for worktree-spawned
    # agents (issue #113). A surface with no handle (background) records what
    # it has and the supervisor polls.
    messages.record_spawn(
        project_root,
        issue,
        surface=spawned.surface,
        handle=spawned.handle,
        pid=spawned.pid,
        log_path=spawned.log_path,
        log=log,
    )
    return Delegated(spawned=spawned, worktree=wt_path, hook_path=hook_path)
