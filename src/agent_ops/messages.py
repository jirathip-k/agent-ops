"""Push notifications between a dispatched run and whoever is waiting on it.

`runs.py` derives a run's state from indirect signals — a live process in
`ps`, a `fix/issue-N` worktree, a halt file, an open PR. Every one is a proxy,
and a dispatched run has no way to simply *say* it finished (issue #98). This
module is that channel: a run reports its own terminal state over Orca's
orchestration bus, and a supervisor blocked in `agent runs --wait` wakes on it
instead of waiting out the poll interval and re-deriving the same verdict.

Two properties this module exists to preserve:

- **Orca is optional.** Every entry point resolves to a no-op when Orca is
  absent, when the run went to a surface with no message bus (background, and
  the CI lane, which has no Orca at all), or when the handle it has on file
  points at a terminal Orca has since forgotten. All three collapse to "no
  message", which is precisely today's behaviour. Nothing here is ever
  required, and nothing here is a new install.
- **Not a state store** (ADR 0003). A message is read once and is never the
  only record of anything: the durable answers stay in GitHub and in the
  outcome record under `.agent-runs/`. Losing a message costs a supervisor one
  poll interval of latency, never a run.

The spawn record (`.agent-runs/issue-N-spawn.json`) is deliberately *not* one
of `runs.discover_runs`'s *candidate-discovery* patterns — it names no issue
`_matches` a regex against, so a spawn record alone never turns an issue into
a run row. It remains an address book, not evidence that a run exists in the
first place. It is, however, a *classification* input (issue #116):
`runs._spawn_liveness` reads it, once an issue is already a candidate from
some other signal, to tell whether the process or terminal it names is still
alive — the piece that used to be missing for `agent spawn`, whose actual OS
process is the runtime CLI (`claude`/`codex`), never `agent` itself.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_ops import orca
from agent_ops.utils import CommandError, run

# The message types a dispatched run sends and a supervisor listens for.
# `worker_done` means the run is over; `escalation` means it stopped early and
# needs a human before it can continue. Both are terminal for waiting
# purposes — the split is what makes "blocked" distinguishable from
# "finished" for the Orca UI and for any other coordinator on the bus.
MESSAGE_TYPES = ("worker_done", "escalation")

# state -> message type. `halted` is the one outcome a human must act on
# (`agent resume`), so it goes out as an escalation.
_TYPE_BY_STATE = {"done": "worker_done", "failed": "worker_done", "halted": "escalation"}

# Orca sets this in every terminal it creates. Only a fallback: the spawn
# record is authoritative, because its handle is by construction the one the
# supervisor is watching. A run whose parent shell happened to be an Orca
# terminal would otherwise report to that terminal instead, which nobody
# checks — harmless, but not useful.
_HANDLE_ENV = "ORCA_TERMINAL_HANDLE"

# Headroom over the `--timeout-ms` we hand Orca, so `utils.run`'s own bound
# only ever fires when the CLI itself has wedged rather than racing its
# advertised timeout.
_WAIT_GRACE_S = 10.0

# Non-blocking calls (a `--unread` drain, a send) should be instant, so they
# take a tighter bound than `utils.DEFAULT_TIMEOUT_S`: both callers are inside
# something that is itself waiting — a poll loop with its own cadence, and a
# stop hook running in a session's critical path, where a command that does not
# return holds the session open (issue #113).
_CHECK_TIMEOUT_S = 30.0


@dataclass(frozen=True)
class SpawnRecord:
    """How to address one dispatched run, written at dispatch time.

    `handle` is the Orca terminal the run lives in — the one stable,
    machine-readable identity of a dispatched worker. `pid`/`log_path` are the
    background surface's equivalent; they are recorded so the record describes
    every surface, not only the one with a message bus.
    """

    issue: int
    surface: str
    handle: str | None = None
    pid: int | None = None
    log_path: str | None = None
    spawned_at: float = 0.0


@dataclass(frozen=True)
class RunMessage:
    """A run's own report of how it ended."""

    issue: int
    kind: str  # one of MESSAGE_TYPES
    state: str  # done | halted | failed — the vocabulary `runs.Run.state` uses
    pr_url: str | None = None
    reason: str | None = None

    @property
    def detail(self) -> str:
        """A `runs.Run.detail`-shaped one-liner for the transition log."""
        if self.pr_url is not None:
            return f"reported {self.state} — {self.pr_url}"
        if self.reason:
            return f"reported {self.state} — {self.reason}"
        return f"reported {self.state}"


def spawn_path(project_root: Path, issue: int) -> Path:
    return project_root / ".agent-runs" / f"issue-{issue}-spawn.json"


def record_spawn(
    project_root: Path,
    issue: int,
    *,
    surface: str,
    handle: str | None = None,
    pid: int | None = None,
    log_path: Path | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> None:
    """Persist how to reach this run, overwriting any previous cycle's record.

    Overwriting is what scopes the mailbox to the current cycle: every `orca
    terminal create` mints a fresh handle, so a stale message left unread by
    an earlier cycle sits on a handle nobody looks at any more.

    Best-effort. A record that cannot be written costs the fast path, not the
    run — the supervisor falls back to polling, which is all it ever had.
    """
    path = spawn_path(project_root, issue)
    payload = {
        "issue": issue,
        "surface": surface,
        "handle": handle,
        "pid": pid,
        "log_path": str(log_path) if log_path is not None else None,
        "spawned_at": time.time(),
    }
    try:
        path.parent.mkdir(exist_ok=True)
        # Temp-then-replace: a supervisor may read this at any moment, and a
        # torn half-written file would be indistinguishable from a corrupt one.
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload))
        tmp_path.replace(path)
    except OSError as exc:
        log(f"warning: could not record the spawn of #{issue} at {path}: {exc}")


def load_spawn(
    project_root: Path, issue: int, *, log: Callable[[str], None] = lambda _: None
) -> SpawnRecord | None:
    """The spawn record for `issue`, or None when there isn't a usable one.

    Missing is the ordinary case (a run started before this existed, or one
    dispatched from somewhere that never wrote a record) and is silent.
    Unreadable or malformed warns once and is then treated as missing: a
    supervisor with no address falls back to polling.
    """
    path = spawn_path(project_root, issue)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise TypeError("spawn record is not a JSON object")
        handle = data.get("handle")
        pid = data.get("pid")
        return SpawnRecord(
            issue=issue,
            surface=str(data.get("surface") or "unknown"),
            handle=str(handle) if handle else None,
            pid=int(pid) if pid is not None else None,
            log_path=str(data["log_path"]) if data.get("log_path") else None,
            spawned_at=float(data.get("spawned_at") or 0.0),
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        log(f"warning: could not read the spawn record for #{issue} at {path}: {exc}")
        return None


def _handle_for(project_root: Path, issue: int, *, log: Callable[[str], None]) -> str | None:
    """The terminal handle to address this run's messages to, if there is one."""
    record = load_spawn(project_root, issue, log=log)
    if record is not None and record.handle:
        return record.handle
    return os.environ.get(_HANDLE_ENV) or None


def send_outcome(
    project_root: Path,
    issue: int,
    *,
    state: str,
    pr_url: str | None = None,
    reason: str | None = None,
    log: Callable[[str], None] = lambda _: None,
) -> bool:
    """Tell whoever dispatched this run that it reached `state`.

    The payload is `issue-N-outcome.json`'s record plus the issue number that
    addresses it, so the pushed fact and the durable one are one schema rather
    than two: `state`, `pr_url`, `reason`, `finished_at`, same names and same
    `None` conventions.

    Best-effort in exactly the sense `_record_halt` means it. Returns whether
    a message actually went out, which callers may ignore — no Orca, no
    handle, or a send that fails is a dropped notification, and the supervisor
    still reaches the same verdict from polling one interval later.
    """
    if not orca.available():
        return False
    handle = _handle_for(project_root, issue, log=log)
    if handle is None:
        return False
    payload = {
        "issue": issue,
        "state": state,
        "pr_url": pr_url,
        "reason": reason,
        "finished_at": time.time(),
    }
    summary = pr_url or reason or "no further detail"
    cmd = [
        orca.executable(),
        "orchestration",
        "send",
        "--to",
        handle,
        "--from",
        handle,
        "--type",
        _TYPE_BY_STATE.get(state, "worker_done"),
        "--subject",
        f"agent-ops issue #{issue}: {state}",
        "--body",
        f"#{issue} {state} — {summary}",
        "--payload",
        json.dumps(payload),
        "--json",
    ]
    try:
        proc = run(cmd, check=False, timeout=_CHECK_TIMEOUT_S)
    except (CommandError, OSError) as exc:
        # OSError: `utils.run` lets `FileNotFoundError` through when the
        # executable has gone since `available()` said otherwise — the same
        # hole `_record_halt` documents for a missing `gh`. CommandError: the
        # timeout bound fired on a wedged CLI. This function promises not to
        # crash a finished run, so both are caught here rather than at every
        # call site.
        log(f"could not push the {state!r} outcome for #{issue} to {handle}: {exc}")
        return False
    if proc.returncode != 0:
        log(
            f"could not push the {state!r} outcome for #{issue} to {handle}: "
            f"{proc.stderr.strip() or proc.stdout.strip() or 'no output'}"
        )
        return False
    return True


def _parse(message: dict[str, Any], issue: int) -> RunMessage | None:
    """One raw bus message → a `RunMessage`, or None if it isn't ours.

    Orca carries `payload` as a JSON *string*. Anything unparseable, or
    addressed to a different issue than the mailbox we read it from, is
    dropped rather than guessed at — a message is a shortcut, so a doubtful
    one is worth less than the polling answer that follows it.
    """
    try:
        payload = json.loads(message.get("payload") or "{}")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("issue") != issue:
        return None
    state = payload.get("state")
    if not isinstance(state, str) or not state:
        return None
    pr_url = payload.get("pr_url")
    reason = payload.get("reason")
    return RunMessage(
        issue=issue,
        kind=str(message.get("type") or "worker_done"),
        state=state,
        pr_url=str(pr_url) if pr_url else None,
        reason=str(reason) if reason else None,
    )


def _check(
    handle: str,
    *,
    mode: str,
    wait_ms: int | None,
    timeout_s: float,
    log: Callable[[str], None],
) -> list[dict[str, Any]] | None:
    """One `orca orchestration check`; None when the call itself failed.

    A handle Orca no longer knows is not an error: `check` returns an empty
    list for it, which is why a stale record degrades to the poll cadence
    rather than to a crash.
    """
    cmd = [
        orca.executable(),
        "orchestration",
        "check",
        "--terminal",
        handle,
        mode,
        "--types",
        ",".join(MESSAGE_TYPES),
        "--json",
    ]
    if wait_ms is not None:
        cmd += ["--wait", "--timeout-ms", str(wait_ms)]
    try:
        proc = run(cmd, check=False, timeout=timeout_s)
    except (CommandError, OSError) as exc:
        # CommandError: the `timeout_s` bound fired (a wedged CLI). OSError:
        # the executable vanished since `available()` was asked. Neither is
        # worth failing a poll loop over — both read as "no news".
        log(f"warning: could not check messages for {handle}: {exc}")
        return None
    if proc.returncode != 0:
        log(
            f"warning: could not check messages for {handle}: "
            f"{proc.stderr.strip() or proc.stdout.strip() or 'no output'}"
        )
        return None
    try:
        result = json.loads(proc.stdout).get("result") or {}
        found = result.get("messages")
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        log(f"warning: unreadable response from `orca orchestration check`: {exc}")
        return None
    return [m for m in found if isinstance(m, dict)] if isinstance(found, list) else []


def collect(
    project_root: Path,
    issues: Iterable[int],
    *,
    log: Callable[[str], None] = lambda _: None,
) -> dict[int, RunMessage]:
    """Drain any outcome each of `issues` has reported, newest report winning.

    Reads with `--unread`, which marks the messages read — this is a one-shot
    consume, which is why only a supervisor that is actually waiting calls it.
    A one-shot snapshot (`agent runs`) must not, or it would show a result
    once and lose it; that is the durable outcome record's job.

    Issues with no Orca, no spawn record and no handle are simply absent from
    the result, which reads as "nothing reported" — the same as a run that
    never sent anything.
    """
    if not orca.available():
        return {}
    found: dict[int, RunMessage] = {}
    for issue in issues:
        handle = _handle_for(project_root, issue, log=log)
        if handle is None:
            continue
        raw = _check(handle, mode="--unread", wait_ms=None, timeout_s=_CHECK_TIMEOUT_S, log=log)
        if not raw:
            continue
        for message in raw:
            parsed = _parse(message, issue)
            if parsed is not None:
                found[issue] = parsed  # later message wins
    return found


def wait_for_message(
    project_root: Path,
    issue: int,
    timeout_s: float,
    *,
    log: Callable[[str], None] = lambda _: None,
) -> bool:
    """Block up to `timeout_s` for `issue` to report, without consuming anything.

    `--peek` on purpose: this only answers "is it worth polling now?", and the
    message itself must survive for the next `collect` to read and parse.

    Returns True only when something is actually waiting. False covers every
    other case — no Orca, no handle, a handle Orca has forgotten, a wedged
    CLI, or the timeout simply expiring — so a caller can treat False as "no
    news" and carry on polling. Callers must not assume the full `timeout_s`
    elapsed: an unavailable channel returns immediately.
    """
    if timeout_s <= 0 or not orca.available():
        return False
    handle = _handle_for(project_root, issue, log=log)
    if handle is None:
        return False
    raw = _check(
        handle,
        mode="--peek",
        wait_ms=int(timeout_s * 1000),
        timeout_s=timeout_s + _WAIT_GRACE_S,
        log=log,
    )
    return bool(raw)
