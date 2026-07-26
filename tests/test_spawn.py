"""`agent spawn` / `agent report` — the reporting channel for worktree agents.

The three cases issue #113 names are covered end to end: a worker that
completes and says so, a worker that exits without reporting (the case a prompt
instruction misses), and Orca being absent entirely.
"""

import json
import shlex
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops import messages, orca, runs, surfaces
from agent_ops.cli import app
from agent_ops.runtimes import claude_code, codex
from agent_ops.utils import CommandError, run
from agent_ops.workflows import spawn as spawn_module

runner = CliRunner()


def _fake_terminal_create(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    payload = {"result": {"terminal": {"handle": "term_first"}}}
    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    run(["git", "init", "-b", "main"], cwd=tmp_path)
    run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    run(["git", "config", "user.name", "test"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("hello\n")
    run(["git", "add", "."], cwd=tmp_path)
    run(["git", "commit", "-m", "init"], cwd=tmp_path)
    return tmp_path


class FakeSurface:
    """A surface that records what it was asked to spawn and hands back a handle.

    `on_spawn` runs at the moment the session would start, which is how the
    ordering assertions check that the stop hook was already on disk by then.
    """

    name = "fake"

    def __init__(self, handle: str | None = "term_worker") -> None:
        self.calls: list[tuple[str, list[str], Path, Path | None]] = []
        self.handle = handle
        self.on_spawn: list[str] = []

    def available(self) -> bool:
        return True

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> surfaces.Spawned:
        self.calls.append((label, command, cwd, attach_path))
        settings = cwd / claude_code.SETTINGS_REL
        self.on_spawn.append("hook seeded" if settings.is_file() else "no hook yet")
        return surfaces.Spawned(where="fake surface", surface=self.name, handle=self.handle)


def _claude_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_code.ClaudeCodeRuntime, "available", lambda self: True)


def _fake_surface(
    monkeypatch: pytest.MonkeyPatch, handle: str | None = "term_worker"
) -> FakeSurface:
    fake = FakeSurface(handle)
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": fake)
    return fake


def _orca_on(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Orca available, with every `messages` shell-out recorded instead of run."""
    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "executable", lambda: "orca")
    calls: list[list[str]] = []

    def fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(messages, "run", fake)
    return calls


# Claude Code events that fire while the agent is still working. Issue #120:
# the hook was seeded on `Stop`, which fires whenever the assistant finishes
# speaking — so `--if-unreported` bound the single report slot to the first
# turn and every spawned agent was recorded `halted` a minute in.
PER_TURN_EVENTS = ("Stop", "SubagentStop", "PostToolUse", "PreToolUse", "UserPromptSubmit")


def _hook_command(worktree: Path) -> list[str]:
    """The argv the seeded session-end hook will actually run."""
    settings = json.loads((worktree / claude_code.SETTINGS_REL).read_text())
    return shlex.split(settings["hooks"]["SessionEnd"][0]["hooks"][0]["command"])


def _fire_stop_hook(worktree: Path) -> None:
    """Run the seeded hook command through the CLI, as Claude Code would.

    Going through the seeded string rather than calling `report_outcome`
    directly is the point: it proves the command written into the settings file
    is one this CLI can actually parse and execute.
    """
    argv = _hook_command(worktree)
    assert argv[0] == "agent"
    result = runner.invoke(app, argv[1:])
    assert result.exit_code == 0, result.output


# --- spawning --------------------------------------------------------------


def test_spawn_records_the_handle_so_a_supervisor_has_somewhere_to_listen(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The link that was missing entirely for worktree-spawned agents (#113)."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)

    result = runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    assert result.exit_code == 0, result.output
    record = messages.load_spawn(repo.resolve(), 113)
    assert record is not None
    assert record.handle == "term_worker"
    assert record.surface == "fake"


def test_spawn_seeds_the_stop_hook_before_the_session_starts(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code snapshots hooks at startup, so seeding after would be a no-op."""
    _claude_installed(monkeypatch)
    fake = _fake_surface(monkeypatch)

    runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    assert fake.on_spawn == ["hook seeded"]
    _label, _command, cwd, attach = fake.calls[0]
    wt = repo.resolve() / ".worktrees" / "issue-113"
    # The session's shell has to start *in* the worktree — unlike `dispatch`,
    # whose child `agent implement` finds its own worktree.
    assert cwd == wt
    assert attach == wt


def test_spawn_seeds_a_hook_that_this_cli_can_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)

    runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    wt = repo.resolve() / ".worktrees" / "issue-113"
    assert _hook_command(wt) == spawn_module.report_command(repo.resolve(), 113)
    settings = json.loads((wt / claude_code.SETTINGS_REL).read_text())
    assert set(settings["hooks"]) == set(claude_code.STOP_HOOK_EVENTS)
    # ...and the worker may report a better outcome by hand without a prompt.
    assert "Bash(agent report:*)" in settings["permissions"]["allow"]


def test_spawn_keeps_its_wiring_out_of_the_workers_own_commit(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A linked worktree has no private `info/exclude`, so the files ignore themselves."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)

    runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    wt = repo.resolve() / ".worktrees" / "issue-113"
    status = run(["git", "status", "--porcelain"], cwd=wt)
    assert status.stdout.strip() == ""


def test_spawn_passes_the_brief_to_the_interactive_command(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claude_installed(monkeypatch)
    fake = _fake_surface(monkeypatch)

    runner.invoke(app, ["spawn", "113", "--project", str(repo), "--prompt", "rebase the branch"])

    _label, command, _cwd, _attach = fake.calls[0]
    assert command[0] == "claude"
    assert "-p" not in command  # interactive, not the headless loop's form
    assert "rebase the branch" in command[-1]
    # The brief advertises reporting as an upgrade, not as the mechanism.
    assert "agent report 113" in command[-1]


def test_spawn_gives_the_session_a_permission_mode_it_can_work_under(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug in #115: no mode meant the worker stopped on every edit, and the
    stop hook then reported a worker waiting for one click as `halted`."""
    _claude_installed(monkeypatch)
    fake = _fake_surface(monkeypatch)

    runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    _label, command, _cwd, _attach = fake.calls[0]
    assert command[command.index("--permission-mode") + 1] == "bypassPermissions"


def test_spawn_takes_the_mode_from_the_project_config(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claude_installed(monkeypatch)
    fake = _fake_surface(monkeypatch)
    config = repo / ".agent" / "config.yaml"
    config.parent.mkdir()
    config.write_text("runtime:\n  interactive_permission_mode: acceptEdits\n")

    runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    _label, command, _cwd, _attach = fake.calls[0]
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"


def test_a_single_spawn_can_tighten_the_mode(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _claude_installed(monkeypatch)
    fake = _fake_surface(monkeypatch)

    result = runner.invoke(
        app, ["spawn", "113", "--project", str(repo), "--permission-mode", "plan"]
    )

    assert result.exit_code == 0, result.output
    _label, command, _cwd, _attach = fake.calls[0]
    assert command[command.index("--permission-mode") + 1] == "plan"


def test_a_mode_the_runtime_cannot_use_fails_before_a_worktree_exists(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise the session dies on the flag before its stop hook loads — the
    worker looks like it vanished, which is the failure this path exists to stop."""
    _claude_installed(monkeypatch)
    fake = _fake_surface(monkeypatch)

    result = runner.invoke(
        app, ["spawn", "113", "--project", str(repo), "--permission-mode", "vibes"]
    )

    assert result.exit_code == 1
    assert "vibes" in result.output
    assert fake.calls == []
    assert not (repo.resolve() / ".worktrees" / "issue-113").exists()


def test_spawn_reuses_a_pristine_worktree_so_a_retry_is_cheap(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)

    assert runner.invoke(app, ["spawn", "113", "--project", str(repo)]).exit_code == 0
    second = runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    assert second.exit_code == 0, second.output
    wt = repo.resolve() / ".worktrees" / "issue-113"
    settings = json.loads((wt / claude_code.SETTINGS_REL).read_text())
    # Re-spawning must not stack a second copy of the same hook.
    assert len(settings["hooks"]["SessionEnd"]) == 1


def test_spawn_says_so_when_the_runtime_has_no_stop_hook(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex can be spawned, but its completion can only ever be inferred."""
    monkeypatch.setattr(codex.CodexRuntime, "available", lambda self: True)
    fake = _fake_surface(monkeypatch)
    config = repo / ".agent" / "config.yaml"
    config.parent.mkdir()
    config.write_text("model_tiers:\n  codex:\n    fast: gpt-5-codex\n")

    result = runner.invoke(app, ["spawn", "113", "--project", str(repo), "--runtime", "codex"])

    assert result.exit_code == 0, result.output
    assert "no stop hook" in result.output
    command = fake.calls[0][1]
    assert command[0] == "codex"
    # ...and the mode still reaches it, in the vocabulary codex actually takes.
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert not (repo.resolve() / ".worktrees" / "issue-113" / claude_code.SETTINGS_REL).exists()


def test_spawn_rejects_two_ways_of_saying_the_same_thing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    brief = repo / "brief.md"
    brief.write_text("from a file")

    result = runner.invoke(
        app,
        ["spawn", "113", "--project", str(repo), "--prompt", "inline", "--prompt-file", str(brief)],
    )

    assert result.exit_code == 1


# --- case 1: the worker finishes and reports -------------------------------


def test_a_worker_that_reports_done_is_recorded_and_pushed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])
    calls = _orca_on(monkeypatch)

    result = runner.invoke(
        app,
        ["report", "113", "--project", str(repo), "--state", "done", "--pr", "https://x/pull/9"],
    )

    assert result.exit_code == 0, result.output
    record = json.loads(runs.outcome_path(repo.resolve(), 113).read_text())
    assert record["state"] == "done"
    assert record["pr_url"] == "https://x/pull/9"
    # ...and it went to the handle the spawn recorded, as a `worker_done`.
    assert calls[0][calls[0].index("--to") + 1] == "term_worker"
    assert calls[0][calls[0].index("--type") + 1] == "worker_done"


def test_a_reported_outcome_is_what_agent_runs_shows(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The durable half: the record outlives the message and the worktree."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])
    runner.invoke(
        app,
        ["report", "113", "--project", str(repo), "--state", "done", "--pr", "https://x/pull/9"],
    )

    result = runner.invoke(app, ["runs", "113", "--project", str(repo)])

    assert "done" in result.output
    assert "PR #9" in result.output


# --- case 2: the worker exits without reporting ----------------------------


def test_the_stop_hook_reports_a_worker_that_said_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case a prompt instruction misses: died, interrupted, or gave up early."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])
    calls = _orca_on(monkeypatch)

    _fire_stop_hook(repo.resolve() / ".worktrees" / "issue-113")

    record = json.loads(runs.outcome_path(repo.resolve(), 113).read_text())
    assert record["state"] == spawn_module.SILENT_EXIT_STATE
    assert record["reason"] == spawn_module.SILENT_EXIT_REASON
    # `escalation`, not `worker_done`: nobody knows whether the work landed,
    # which is a question for a human — and it is what makes "stopped early"
    # distinguishable from "finished" on the bus.
    assert calls[0][calls[0].index("--type") + 1] == "escalation"


def test_the_stop_hook_never_overwrites_a_worker_that_spoke_for_itself(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])
    runner.invoke(
        app,
        ["report", "113", "--project", str(repo), "--state", "done", "--pr", "https://x/pull/9"],
    )
    calls = _orca_on(monkeypatch)

    wt = repo.resolve() / ".worktrees" / "issue-113"
    _fire_stop_hook(wt)
    _fire_stop_hook(wt)  # a re-opened session ends a second time

    assert json.loads(runs.outcome_path(repo.resolve(), 113).read_text())["state"] == "done"
    assert calls == []


def test_no_hook_is_wired_to_a_turn_boundary(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #120, at the wiring: `Stop` fires whenever the assistant finishes
    speaking, not when it gets stuck. Seeding the report there — with
    `--if-unreported` binding the one report slot to the first caller —
    guaranteed the earliest and least informative verdict was the one that
    stuck, so every spawned agent was recorded `halted` about a turn in."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)

    runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    wt = repo.resolve() / ".worktrees" / "issue-113"
    seeded = set(json.loads((wt / claude_code.SETTINGS_REL).read_text())["hooks"])
    assert seeded == {"SessionEnd"}
    assert not seeded & set(PER_TURN_EVENTS)


def test_a_multi_turn_session_is_not_recorded_halted_while_it_works(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reproduction in #120: five files modified, nothing committed, agent
    mid-edit — and `.agent-runs/issue-N-outcome.json` already saying `halted`.

    A turn boundary is not observable from here, so this asserts the thing that
    makes one harmless: nothing the session does before it ends writes a
    verdict, because nothing is wired to anything but its end."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])
    wt = repo.resolve() / ".worktrees" / "issue-113"
    record = runs.outcome_path(repo.resolve(), 113)

    # However many turns the worker takes, no hook of ours runs at their edges.
    hooks = json.loads((wt / claude_code.SETTINGS_REL).read_text())["hooks"]
    for event in PER_TURN_EVENTS:
        assert event not in hooks
    assert not record.is_file()

    # ...and the verdict still arrives when the session actually ends.
    _fire_stop_hook(wt)
    assert json.loads(record.read_text())["state"] == spawn_module.SILENT_EXIT_STATE


def test_the_silent_exit_reason_claims_only_what_the_hook_observed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It used to say "finished, gave up, or waiting for input" — a guess at
    three states it could not tell apart, asserted at the first turn boundary
    of a healthy run (#120)."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    _fire_stop_hook(repo.resolve() / ".worktrees" / "issue-113")

    reason = json.loads(runs.outcome_path(repo.resolve(), 113).read_text())["reason"]
    assert "session ended" in reason
    assert "waiting for input" not in reason


def test_a_second_spawn_clears_the_last_cycles_verdict(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise `--if-unreported` would silence the new cycle's hook forever."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])
    runner.invoke(app, ["report", "113", "--project", str(repo), "--state", "done"])

    runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    assert not runs.outcome_path(repo.resolve(), 113).is_file()
    _fire_stop_hook(repo.resolve() / ".worktrees" / "issue-113")
    record = json.loads(runs.outcome_path(repo.resolve(), 113).read_text())
    assert record["state"] == spawn_module.SILENT_EXIT_STATE


def test_report_exits_zero_even_when_it_cannot_do_anything(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `Stop` hook's non-zero exit is fed back to the agent as work to do."""

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(spawn_module, "report_outcome", boom)

    result = runner.invoke(app, ["report", "113", "--project", str(repo), "--state", "done"])
    assert result.exit_code == 0

    bad = runner.invoke(app, ["report", "113", "--project", str(repo), "--state", "vibes"])
    assert bad.exit_code == 0


# --- who gets told ---------------------------------------------------------


def test_spawn_records_the_terminal_that_asked_for_the_work(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #122: `SpawnRecord` had no field for the spawner, so a completion
    report had nowhere to go but the worker's own handle."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    monkeypatch.setenv(messages._HANDLE_ENV, "term_coordinator")

    runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    record = messages.load_spawn(repo.resolve(), 113)
    assert record is not None
    assert record.spawner == "term_coordinator"
    assert record.handle == "term_worker"
    assert record.kind == messages.SPAWN_KIND


def test_a_completion_report_goes_to_the_spawner_not_into_the_workers_own_session(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Observed on #108: the worker received its own `worker_done` and had to
    spend a turn reasoning about it, while nobody else was told anything."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    monkeypatch.setenv(messages._HANDLE_ENV, "term_coordinator")
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])
    calls = _orca_on(monkeypatch)

    runner.invoke(
        app,
        ["report", "113", "--project", str(repo), "--state", "done", "--pr", "https://x/pull/9"],
    )

    (cmd,) = calls
    assert cmd[cmd.index("--to") + 1] == "term_coordinator"
    assert cmd[cmd.index("--from") + 1] == "term_worker"


def test_a_supervisor_waits_on_the_mailbox_the_report_was_sent_to(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both ends have to agree, or addressing the spawner would silently break
    `agent runs --wait`, which reads a mailbox to know the run is over."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    monkeypatch.setenv(messages._HANDLE_ENV, "term_coordinator")
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])
    calls = _orca_on(monkeypatch)

    messages.wait_for_message(repo.resolve(), 113, 1.0)

    (cmd,) = calls
    assert cmd[cmd.index("--terminal") + 1] == "term_coordinator"


def test_a_run_started_by_hand_keeps_todays_pollable_mailbox(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Orca terminal to inherit an identity from, so there is no spawner —
    which must degrade to the per-run mailbox, not fail (#122)."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])
    calls = _orca_on(monkeypatch)

    record = messages.load_spawn(repo.resolve(), 113)
    assert record is not None and record.spawner is None
    runner.invoke(app, ["report", "113", "--project", str(repo), "--state", "done"])

    (cmd,) = calls
    assert cmd[cmd.index("--to") + 1] == "term_worker"


# --- `--pr` is a URL, or it is refused -------------------------------------


def test_a_bare_pr_number_is_expanded_into_a_url(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--pr 121` stored `{"pr_url": "121"}` and `agent runs` rendered
    `PR #121 — 121` (#122). The field is named `_url`; it should hold one."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    run(["git", "remote", "add", "origin", "git@github.com:jirathip-k/agent-ops.git"], cwd=repo)
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    result = runner.invoke(
        app, ["report", "113", "--project", str(repo), "--state", "done", "--pr", "121"]
    )

    assert result.exit_code == 0, result.output
    record = json.loads(runs.outcome_path(repo.resolve(), 113).read_text())
    assert record["pr_url"] == "https://github.com/jirathip-k/agent-ops/pull/121"
    rendered = runner.invoke(app, ["runs", "113", "--project", str(repo)]).output
    assert "PR #121 — https://github.com/jirathip-k/agent-ops/pull/121" in rendered


def test_a_pr_value_that_is_neither_a_url_nor_a_number_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch)
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    result = runner.invoke(
        app, ["report", "113", "--project", str(repo), "--state", "done", "--pr", "soon"]
    )

    # Exit 0 like every other `report` path — it runs from a hook, where a
    # non-zero exit is fed back to the agent as work to do.
    assert result.exit_code == 0
    assert "neither a PR URL" in result.output
    assert not runs.outcome_path(repo.resolve(), 113).is_file()


# --- one worktree, one agent ----------------------------------------------


def test_spawning_onto_a_worktree_that_already_has_a_session_is_refused(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two agents on one branch is what #117's orphaned terminal produced, and
    it is never a valid end state: both carry the same stop hook, and whichever
    exits first claims the single report slot."""
    _claude_installed(monkeypatch)
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": surfaces.OrcaSurface())
    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "await_indexed", lambda paths: True)
    monkeypatch.setattr(surfaces, "run", _fake_terminal_create)
    monkeypatch.setattr(orca, "terminal_handles", lambda path: set())

    assert runner.invoke(app, ["spawn", "113", "--project", str(repo)]).exit_code == 0

    monkeypatch.setattr(orca, "terminal_handles", lambda path: {"term_first"})
    second = runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    assert second.exit_code == 1
    assert "term_first" in second.output
    assert "second agent" in second.output


def test_an_orca_that_cannot_be_asked_does_not_block_a_spawn(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing on an unknown would make an Orca blip unable to start work,
    which is a worse failure than the one being prevented."""
    _claude_installed(monkeypatch)
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": surfaces.OrcaSurface())
    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "await_indexed", lambda paths: True)
    monkeypatch.setattr(surfaces, "run", _fake_terminal_create)
    monkeypatch.setattr(orca, "terminal_handles", lambda path: None)

    assert runner.invoke(app, ["spawn", "113", "--project", str(repo)]).exit_code == 0
    assert runner.invoke(app, ["spawn", "113", "--project", str(repo)]).exit_code == 0


def test_a_spawn_that_cannot_attach_says_what_it_left_behind(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#117 left `.worktrees/issue-N` on `fix/issue-N`, indexed by Orca, with a
    seeded settings file, no agent, and no explanation."""
    _claude_installed(monkeypatch)

    class DeadSurface(FakeSurface):
        def spawn(self, label, command, cwd, attach_path=None):  # type: ignore[no-untyped-def]
            raise CommandError("`orca terminal create` failed:\nnope")

    monkeypatch.setattr(surfaces, "pick", lambda name="auto": DeadSurface())

    result = runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    assert result.exit_code == 1
    wt = repo.resolve() / ".worktrees" / "issue-113"
    assert wt.is_dir()  # kept on purpose: a retry reuses it
    assert str(wt) in result.output
    assert "agent spawn 113" in result.output  # how to retry
    assert "agent worktree remove issue-113" in result.output  # how to drop it


# --- case 3: no Orca -------------------------------------------------------


def test_spawn_works_with_no_orca_and_the_caller_just_polls(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`orca.available()` is False for the whole suite by default (see conftest)."""
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch, handle=None)

    result = runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    assert result.exit_code == 0, result.output
    assert "no push channel" in result.output
    record = messages.load_spawn(repo.resolve(), 113)
    assert record is not None and record.handle is None
    # Nothing to wake on, and nothing that raises for asking.
    assert messages.wait_for_message(repo.resolve(), 113, 5.0) is False


def test_reporting_without_orca_still_leaves_the_durable_record(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claude_installed(monkeypatch)
    _fake_surface(monkeypatch, handle=None)
    runner.invoke(app, ["spawn", "113", "--project", str(repo)])

    _fire_stop_hook(repo.resolve() / ".worktrees" / "issue-113")

    record = json.loads(runs.outcome_path(repo.resolve(), 113).read_text())
    assert record["state"] == spawn_module.SILENT_EXIT_STATE


# --- the seeded settings file ----------------------------------------------


def test_seeding_merges_into_settings_a_repo_already_had(tmp_path: Path) -> None:
    settings = tmp_path / claude_code.SETTINGS_REL
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [{"command": "mine"}]}]}}))

    assert claude_code.write_stop_hook(tmp_path, ["agent", "report", "1"]) == settings

    data = json.loads(settings.read_text())
    assert [e["hooks"][0]["command"] for e in data["hooks"]["SessionEnd"]] == [
        "mine",
        "agent report 1",
    ]


def test_seeding_leaves_a_settings_file_it_cannot_parse_alone(tmp_path: Path) -> None:
    settings = tmp_path / claude_code.SETTINGS_REL
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json")
    logged: list[str] = []

    seeded = claude_code.write_stop_hook(tmp_path, ["agent", "report", "1"], log=logged.append)

    assert seeded is None

    assert settings.read_text() == "{not json"
    assert any("could not read" in line for line in logged)
