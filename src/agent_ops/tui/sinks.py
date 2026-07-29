"""Where `c` (#235) hands the chat handoff off to, beyond the file (#249).

Mirrors `agent_ops.surfaces.Surface`: a `ChatSink` protocol, one thin adapter
per multiplexer, and `FileSink` as the always-works fallback selected by
`available()`. `deliver()` sends through the first sink that can take the
payload, probing in table order; the file sink is last and never fails, so a
bare terminal or an unlisted multiplexer loses nothing relative to before
this issue.

Every sink receives the *same already-rendered* handoff text — the caller
renders once via `agent_ops.tui.data.render_chat_handoff` — so the untrusted
header survives whichever transport delivers it.

Every multiplexer sink is bound by one invariant, arrived at the hard way
across three review rounds that each caught the same failure in a different
sink (self-identification from live focus/process state instead of injected
env; a target picked from a stale or absent self-identity): a sink must
resolve its own identity from injected environment, never from live focus or
process state. If self-identity is unknowable, or if the target is not
provably exactly one pane that is not itself, the sink returns False and the
caller falls through. Delivering to a guessed target is never acceptable —
the payload is untrusted, and the fallback is a file that loses nothing.
Two disciplines follow from that invariant, both required to keep an
untrusted issue body from acting on the neighbouring pane rather than merely
displaying in it:

- **Exactly one other pane, or fall through.** Each sink lists the panes/
  windows/terminals it could target and only sends when precisely one is not
  itself; zero or several is ambiguous and must not guess (an issue body
  landing in the TUI's own pane would drive its `d`/`r`/`q` bindings).
- **Paste, never type, and never press Enter.** A sink hands the payload to
  its multiplexer's paste/stdin path (`utils.run`'s `input_text`, or an argv
  element combined with a literal/bracketed-paste flag) rather than
  synthesising keystrokes, and no sink sends a trailing Enter. This is what
  keeps a body containing backticks, `$(...)`, or newlines arriving as inert
  characters staged in the pane's input rather than executing — but only for
  a receiving program that has bracketed paste enabled (a modern zsh/bash/
  fish, or a chat CLI that turns it on); the receiving pane could just as
  well be a plain shell that never enabled it, in which case the escape bytes
  are ordinary input and each line executes as it arrives. See "Pipeline TUI
  chat handoff" in docs/workflow.md for the operator-facing version of this
  caveat.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Protocol

from agent_ops import orca
from agent_ops.tui import data
from agent_ops.utils import run

# Orca sets this in every terminal it creates (see `messages._HANDLE_ENV`) —
# the only way to tell "the terminal the TUI itself is running in" apart from
# any other terminal open on the same worktree card.
_ORCA_HANDLE_ENV = "ORCA_TERMINAL_HANDLE"


class ChatSink(Protocol):
    name: str

    def available(self) -> bool: ...

    def send(self, text: str) -> bool:
        """Deliver `text` verbatim. False on any failure — the caller then
        tries the next sink in the table, never raises."""
        ...


class TmuxSink:
    """A neighbouring pane in the same tmux window."""

    name = "tmux"

    def __init__(self, env: dict[str, str]) -> None:
        self._env = env

    def available(self) -> bool:
        return bool(self._env.get("TMUX"))

    def _target_pane(self) -> str | None:
        # Self-identify via `$TMUX_PANE` from the injected env, the same
        # race-free pattern `WeztermSink`/`KittySink` use — not "whichever
        # pane `#{pane_active}` reports right now". The probe runs in a
        # thread worker after a `gh` call that can take seconds, during which
        # the operator can easily have clicked into the TUI's own pane;
        # `#{pane_active}` would then describe *that* pane as active and this
        # sink would paste the untrusted body into the TUI's own bindings.
        #
        # An empty `$TMUX_PANE` is unknowable identity, not "nothing to
        # exclude": without this guard a single-pane window reads as one
        # other pane and this sink pastes the untrusted body into the TUI's
        # own pane, the one outcome this module promises cannot happen.
        own = self._env.get("TMUX_PANE", "")
        if not own:
            return None
        # `-t own` anchors the listing to own's window, not "tmux's current
        # window" (the default target with no `-t`) — that default resolves
        # at call time, so it carries the same focus race `$TMUX_PANE` above
        # exists to avoid: an operator who clicked elsewhere during the `gh`
        # call would otherwise list a different window's panes entirely.
        proc = run(["tmux", "list-panes", "-t", own, "-F", "#{pane_id}"], check=False)
        if proc.returncode != 0:
            return None
        others = [pane for pane in proc.stdout.splitlines() if pane and pane != own]
        return others[0] if len(others) == 1 else None

    def send(self, text: str) -> bool:
        pane = self._target_pane()
        if pane is None:
            return False
        # `load-buffer -` reads `text` from stdin into tmux's paste buffer;
        # `paste-buffer -p` inserts it as a bracketed paste (so a shell in
        # the target pane sees it as pasted content, not typed keystrokes —
        # embedded newlines don't submit) and `-d` deletes the buffer after,
        # so no trace of an untrusted body is left sitting in tmux's buffer
        # list. No separate "Enter" send: the paste lands staged, unsubmitted.
        loaded = run(["tmux", "load-buffer", "-"], input_text=text, check=False)
        if loaded.returncode != 0:
            return False
        if run(["tmux", "paste-buffer", "-p", "-d", "-t", pane], check=False).returncode == 0:
            return True
        # `-d` above only deletes the buffer on a successful paste — if
        # `paste-buffer` itself failed (bad target, tmux hiccup), the
        # untrusted body is still sitting as tmux's top paste buffer, one
        # `prefix-]` away from landing in any pane. Clean it up explicitly
        # rather than leaving that behind.
        run(["tmux", "delete-buffer"], check=False)
        return False


class WeztermSink:
    """A neighbouring pane in the same wezterm tab."""

    name = "wezterm"

    def __init__(self, env: dict[str, str]) -> None:
        self._env = env

    def available(self) -> bool:
        return bool(self._env.get("WEZTERM_PANE"))

    def _target_pane(self) -> str | None:
        own = self._env.get("WEZTERM_PANE", "")
        proc = run(["wezterm", "cli", "list", "--format", "json"], check=False)
        if proc.returncode != 0:
            return None
        try:
            panes = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(panes, list):
            return None
        own_tab = next((p.get("tab_id") for p in panes if str(p.get("pane_id")) == own), None)
        if own_tab is None:
            return None
        others = [
            str(p["pane_id"])
            for p in panes
            if p.get("tab_id") == own_tab and str(p.get("pane_id")) != own
        ]
        return others[0] if len(others) == 1 else None

    def send(self, text: str) -> bool:
        pane = self._target_pane()
        if pane is None:
            return False
        # No TEXT argument: `send-text` reads it from stdin instead, and
        # (without `--no-paste`, which this never passes) wraps it as a
        # bracketed paste — a shell in the target pane sees pasted content,
        # not typed keystrokes, so embedded newlines don't submit.
        proc = run(["wezterm", "cli", "send-text", "--pane-id", pane], input_text=text, check=False)
        return proc.returncode == 0


class KittySink:
    """A neighbouring window in the same kitty OS-window tab."""

    name = "kitty"

    def __init__(self, env: dict[str, str]) -> None:
        self._env = env

    def available(self) -> bool:
        return bool(self._env.get("KITTY_WINDOW_ID"))

    def _target_window(self) -> str | None:
        own = self._env.get("KITTY_WINDOW_ID", "")
        proc = run(["kitty", "@", "ls"], check=False)
        if proc.returncode != 0:
            return None
        try:
            os_windows = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        if not isinstance(os_windows, list):
            return None
        for os_window in os_windows:
            for tab in os_window.get("tabs", []):
                ids = [str(w.get("id")) for w in tab.get("windows", [])]
                if own in ids:
                    others = [i for i in ids if i != own]
                    return others[0] if len(others) == 1 else None
        return None

    def send(self, text: str) -> bool:
        window = self._target_window()
        if window is None:
            return False
        # kitty's `send-text` types keystrokes with no paste mode of its
        # own, so the payload is wrapped in the bracketed-paste escape
        # sequences ourselves (kitty's documented idiom for this) and sent
        # via `--stdin` — a shell in the target window sees pasted content,
        # not typed keystrokes, so embedded newlines don't submit.
        payload = f"\x1b[200~{text}\x1b[201~"
        proc = run(
            ["kitty", "@", "send-text", "--match", f"id:{window}", "--stdin"],
            input_text=payload,
            check=False,
        )
        return proc.returncode == 0


class OrcaSink:
    """The one other Orca terminal on this worktree's card, if there is
    exactly one — ambiguous otherwise, so this falls through rather than
    guessing which pane is a chat."""

    name = "orca"

    def __init__(self, project_root: Path, env: dict[str, str]) -> None:
        self._project_root = project_root
        self._env = env

    def available(self) -> bool:
        return orca.available()

    def _target_handle(self) -> str | None:
        handles = orca.terminal_handles(self._project_root)
        if handles is None:
            return None
        # `ORCA_TERMINAL_HANDLE` absent is unknowable identity, not "nothing
        # to exclude": treating every handle as "other" is exactly the
        # guessed-target delivery this module's invariant forbids — with one
        # terminal on the card, that terminal is this one, and this sink
        # would paste the untrusted body into the TUI's own pane.
        own = self._env.get(_ORCA_HANDLE_ENV)
        if not own:
            return None
        # `own` must also appear in the card's live handle listing, not just
        # be non-empty: this sink is only reachable when the TUI is itself
        # running in an Orca terminal, which already follows from the guard
        # above — so if `own` is set but absent from `handles`, the handle is
        # stale or the listing is of a different card, and either way
        # self-identity is not established. Deliberately no cross-card
        # fallback here: an operator outside Orca reaches tmux, wezterm,
        # kitty, or the file sink, and the file sink loses nothing.
        if own not in handles:
            return None
        candidates = handles - {own}
        return next(iter(candidates)) if len(candidates) == 1 else None

    def send(self, text: str) -> bool:
        handle = self._target_handle()
        if handle is None:
            return False
        # No `--enter`: `orca terminal send` has no paste or literal mode,
        # only `--text` and `--enter` (the latter a real Enter keypress that
        # would submit whatever untrusted content just landed) — leaving it
        # off suppresses only the *final* Enter, not any newline embedded in
        # `text` itself, so the payload is also wrapped in the bracketed-paste
        # escapes (kitty's idiom, reused here) the same way `KittySink` does.
        #
        # Verified `--text` transmits bytes verbatim rather than stripping or
        # reinterpreting them: an Orca terminal running `/bin/cat -v`, sent
        # `orca terminal send --text $'\x1b[200~AAA\nBBB\x1b[201~'`, rendered
        # back `^[[200~AAA` / `BBB^[[201~` — the escape bytes arrived intact
        # and unstripped. Re-run that check against a live Orca terminal
        # rather than re-litigating it if this is ever in doubt again.
        payload = f"\x1b[200~{text}\x1b[201~"
        cmd = [orca.executable(), "terminal", "send", "--terminal", handle, "--text", payload]
        proc = run(cmd, check=False)
        return proc.returncode == 0


class FileSink:
    """The always-works fallback: the same `.agent-runs/tui-selection.md`
    drop `c` has written since #235. Never fails, so it is always last."""

    name = "file"

    def __init__(self, path: Path) -> None:
        self._path = path

    def available(self) -> bool:
        return True

    def send(self, text: str) -> bool:
        data.write_rendered_handoff(self._path, text)
        return True


class CommandSink:
    """The `tui.chat_sink` escape hatch: a free-form command with a `{text}`
    placeholder, for a multiplexer not in the table above. `shlex.split` on
    the template, `{text}` substituted as a single argv element — never
    through a shell, same as every other sink."""

    name = "command"

    def __init__(self, template: str) -> None:
        self._template = template
        # Set on failure so a caller (namely `deliver()`) can report *why* the
        # configured sink failed, not just that it did — `None` on success or
        # before `send()` has run.
        self.failure_reason: str | None = None

    def available(self) -> bool:
        return True

    def send(self, text: str) -> bool:
        # `TuiConfig` already refuses a template `shlex.split` chokes on at
        # config load — this catches `ValueError` again anyway so `deliver()`
        # can't crash the TUI if a template reaches here some other way
        # (a future caller that skips validation, a construction directly in
        # a test): "never raise" has to hold here even if load-time
        # validation is ever bypassed.
        try:
            parts = shlex.split(self._template)
        except ValueError as exc:
            self.failure_reason = f"invalid command: {exc}"
            return False
        argv = [text if part == "{text}" else part for part in parts]
        proc = run(argv, check=False)
        if proc.returncode == 0:
            self.failure_reason = None
            return True
        # Same stderr-or-exit-code idiom as `app.py`'s `_exec_worker`. A spawn
        # failure (missing binary) never reaches here as a bare exit code:
        # `utils.run`'s `check=False` contract populates `stderr` with a
        # descriptive "could not be started" message in that case, which is
        # what ends up in `failure_reason` instead of `f"exit 127"` — that's
        # what makes a missing binary distinguishable from an ordinary
        # non-zero exit with nothing on stderr.
        self.failure_reason = proc.stderr.strip() or f"exit {proc.returncode}"
        return False


def deliver(
    text: str, config_value: str | None, env: dict[str, str], project_root: Path
) -> tuple[ChatSink, str | None]:
    """Deliver `text` through the first sink that can take it and return
    `(sink, error)` — the sink that delivered it, and, only when an explicit
    `tui.chat_sink` was configured and failed, a message saying so. The
    returned sink has already received the payload — it is not meant for a
    second `send()`.

    An explicit `tui.chat_sink` wins outright; if it fails, this still falls
    back to `FileSink` (so `text` is never lost), but the operator configured
    a specific sink and it broke — that must be reported, not swallowed
    (issue #252), so `error` names the template and the reason. Auto-probing
    (no `config_value`) working as designed even when it exhausts every
    candidate and lands on the file sink — `error` is always `None` on that
    path.

    With no `config_value`, the table is probed in order (tmux, wezterm,
    kitty, Orca), actually attempting `send()` on each available candidate —
    a multiplexer whose env var is set but whose binary is missing on PATH
    (`available()` true, `send()` false) is skipped rather than trusted, so
    it degrades silently to the next candidate. `FileSink` is last and its
    `send()` never fails, so this always returns having delivered `text`
    somewhere.

    Zellij is deliberately not in this table: `zellij action write-chars`
    hits whichever pane is currently focused, and there is no race-free way
    from here to target a specific *other* pane the way the rest of this
    table does — reachable instead via `tui.chat_sink` for anyone who wants
    it, focus-dependent risk and all.

    `env` is injected rather than read from `os.environ` here so tests (and
    a dev running pytest inside tmux) aren't at the mercy of the real
    environment — callers pass `dict(os.environ)`.
    """
    file_sink = FileSink(data.chat_handoff_path(project_root))
    if config_value:
        override = CommandSink(config_value)
        if override.send(text):
            return override, None
        file_sink.send(text)
        reason = override.failure_reason or "failed"
        return file_sink, f'chat_sink "{config_value}" failed ({reason})'
    for sink in (
        TmuxSink(env),
        WeztermSink(env),
        KittySink(env),
        OrcaSink(project_root, env),
    ):
        if sink.available() and sink.send(text):
            return sink, None
    file_sink.send(text)
    return file_sink, None
