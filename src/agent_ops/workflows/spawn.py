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
- exited without reporting — finished silently, gave up, was interrupted, or
  crashed → the hook's `halted`, "the session ended without reporting"
- still open: working, or finished and sitting at a prompt → nothing is sent,
  and `runs.spawn_state` reports the session as `running` for as long as it
  exists. The hook fires at `SessionEnd` only (issue #120): a hook on every
  turn boundary recorded a healthy agent as `halted` a minute into its run,
  which is worse than not answering
- killed outright (SIGKILL, power loss), or a runtime with no hook mechanism
  (Codex today) → nothing is sent, which is silence: the supervisor's poll
  resolves it exactly as it did before this existed

The report is addressed to whoever spawned the run, not to the run itself —
`messages.SpawnRecord.spawner`, recorded here because this is the only moment
that identity exists (issue #122).

Nothing here is a state store (ADR 0003). The durable answer is the outcome
record and GitHub; the message is a shortcut that saves a supervisor one poll
interval, and dropping it costs latency, never a run.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_ops import messages, orca, runs, surfaces, worktree
from agent_ops.config import load_project_config
from agent_ops.runtimes import get_spawnable_runtime
from agent_ops.utils import CommandError
from agent_ops.workflows.implement import task_identifiers

# The states a worker may report. `done`/`failed` go out as `worker_done`,
# `halted` as an `escalation` — see `messages._TYPE_BY_STATE`. That split is
# what makes "stopped early, needs a human" distinguishable from "finished",
# and both distinguishable from silence.
REPORT_STATES = ("done", "halted", "failed")

# What the session-end hook reports for a worker that never said anything.
# `halted` on purpose: nobody knows whether the work landed, which is a
# question for a human, not a completion.
#
# The reason claims only what the hook actually observed. It used to say
# "finished, gave up, or waiting for input" — a guess covering three states it
# could not distinguish, and issue #120 found it asserted at the first turn
# boundary of a healthy run. Firing at `SessionEnd` alone makes the one claim
# true: the session is over and nothing was reported.
SILENT_EXIT_STATE = "halted"
SILENT_EXIT_REASON = (
    "the session ended without reporting an outcome — check the worktree "
    "before assuming the work landed"
)


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
    """Argv the session-end hook runs when a spawned session ends.

    `--if-unreported` is what keeps the generic report from overwriting a real
    one: a worker that already reported — by hand, with a PR or a blocker —
    silences it. It is no longer load-bearing for *frequency* (issue #120): the
    hook fires once, at session end, rather than at every turn boundary.

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
    permission_mode: str | None = None,
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

    `permission_mode` overrides `runtime.interactive_permission_mode` for this
    one spawn. It is resolved and validated *before* the worktree is created,
    so a bad mode costs nothing to correct: the alternative is a checkout and a
    terminal for a session that dies on an unparsable flag (#115).
    """
    config = load_project_config(project_root)
    role = config.resolve_role("implementer", runtime_override=runtime_name)
    runtime = get_spawnable_runtime(role.runtime)
    if not runtime.available():
        raise RuntimeError(f"Runtime {runtime.name!r} CLI is not installed/on PATH")
    chosen = surfaces.pick(surface_name)
    # Built up front, before anything is created: it is the only step that can
    # still reject its inputs, and an unusable permission mode should not cost
    # a worktree first.
    command = runtime.interactive_command(
        opening_brief(project_root, issue, prompt),
        permission_mode=permission_mode or config.runtime.interactive_permission_mode,
        model=role.model,
    )

    task_id, branch = task_identifiers(issue)
    wt_path = project_root / config.worktree_dir / task_id
    # Checked before the worktree is created, not after: a path that does not
    # exist yet cannot be hosting anything, so this only ever fires for a reuse
    # (`reuse=True` accepts a pristine checkout) and never leaves a worktree
    # behind when it refuses. Two agents on one branch is the end state issue
    # #117 reached the hard way, and nothing about it is recoverable after the
    # fact — the second session is already editing.
    if wt_path.exists():
        _refuse_a_second_session(wt_path, issue, chosen)
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

    try:
        spawned = chosen.spawn(f"agent-spawn-issue-{issue}", command, wt_path, attach_path=wt_path)
    except CommandError as exc:
        # The worktree is fine and the hook is already on it, so a retry is
        # cheap — but a caller left staring at a bare Orca error has no way to
        # know that, and issue #117 watched exactly that turn into an
        # abandoned half-built worktree nobody could account for.
        raise CommandError(
            f"{exc}\n"
            f"nothing is running, but the worktree {wt_path} and branch {branch} were kept "
            f"with the stop hook already seeded. Either:\n"
            f"  retry — `agent spawn {issue} --project {project_root}` reuses this worktree\n"
            f"  start it by hand — cd {wt_path} && {shlex.join(command)}\n"
            f"  drop it — `agent worktree remove {task_id} --project {project_root}`"
        ) from exc
    # How to reach the worker, and who to tell when it is done — the two halves
    # that were missing for worktree-spawned agents (issues #113, #122). A
    # surface with no handle (background) records what it has, and a spawn with
    # no identifiable spawner records None: both degrade to the pollable
    # per-run mailbox rather than failing.
    messages.record_spawn(
        project_root,
        issue,
        surface=spawned.surface,
        handle=spawned.handle,
        pid=spawned.pid,
        log_path=spawned.log_path,
        kind=messages.SPAWN_KIND,
        spawner=messages.current_handle(),
        log=log,
    )
    return Delegated(spawned=spawned, worktree=wt_path, hook_path=hook_path)


def _refuse_a_second_session(wt_path: Path, issue: int, chosen: surfaces.Surface) -> None:
    """Raise if `wt_path` is already hosting an agent session.

    Only the Orca surface can answer this — a background spawn's pid is in the
    spawn record, which the *next* spawn overwrites, so there is nothing to
    read back. An Orca that cannot be asked (`terminal_handles` returns None)
    does not block the spawn: refusing on an unknown would make an Orca blip
    unable to start work, which is a worse failure than the one being
    prevented.
    """
    if not isinstance(chosen, surfaces.OrcaSurface):
        return
    existing = orca.terminal_handles(wt_path)
    if not existing:
        return
    raise CommandError(
        f"{wt_path} already has {len(existing)} live Orca terminal(s) — "
        f"{', '.join(sorted(existing))}. Spawning here would put a second agent on "
        f"{wt_path.name}'s branch, which is never a valid end state (issue #117): both "
        f"would carry the same stop hook, and whichever exits first would claim the "
        f"single report slot for #{issue}.\n"
        f"Use that session, or close it (`orca terminal close --terminal <handle>`) and "
        f"re-run."
    )
