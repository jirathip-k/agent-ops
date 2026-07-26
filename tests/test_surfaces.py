import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from agent_ops import runs, surfaces
from agent_ops.utils import CommandError


def _await_text(log: Path, text: str) -> str:
    for _ in range(50):
        if log.exists() and text in log.read_text():
            break
        time.sleep(0.05)
    return log.read_text()


def test_background_surface_spawns_and_logs(tmp_path: Path) -> None:
    spawned = surfaces.BackgroundSurface().spawn(
        "demo", ["sh", "-c", "echo surface-works"], tmp_path
    )
    assert "background pid" in spawned.where
    log = spawned.log_path
    assert log is not None
    # The printed `where` names the file this run is actually writing, so
    # `tail -f` on it is still correct under per-attempt names (issue #92).
    assert str(log) in spawned.where
    assert log.parent == tmp_path / ".agent-runs"
    assert "surface-works" in _await_text(log, "surface-works")


def test_background_surface_logs_under_cwd_not_attach_path(tmp_path: Path) -> None:
    wt = tmp_path / ".worktrees" / "issue-1"
    wt.mkdir(parents=True)
    surfaces.BackgroundSurface().spawn("demo", ["true"], tmp_path, attach_path=wt)
    # the attach target may be deleted on success; the log must outlive it
    assert list((tmp_path / ".agent-runs").glob("demo-*.log"))
    assert not (wt / ".agent-runs").exists()


def test_background_surface_keeps_both_records_when_a_label_is_spawned_twice(
    tmp_path: Path,
) -> None:
    """Issue #92: the label is derived from the issue number alone, so a
    re-dispatch or an `agent resume` cycle used to truncate the previous
    attempt's only record."""
    first = surfaces.BackgroundSurface().spawn(
        "agent-issue-92", ["sh", "-c", "echo first-attempt"], tmp_path
    )
    second = surfaces.BackgroundSurface().spawn(
        "agent-issue-92", ["sh", "-c", "echo second-attempt"], tmp_path
    )

    assert first.log_path is not None and second.log_path is not None
    assert first.log_path != second.log_path
    assert "first-attempt" in _await_text(first.log_path, "first-attempt")
    assert "second-attempt" in _await_text(second.log_path, "second-attempt")
    # Both attempts are still on disk, and sorting the names orders them —
    # which log belongs to which attempt is readable without opening either.
    logs = sorted(p.name for p in (tmp_path / ".agent-runs").glob("agent-issue-92-*.log"))
    assert logs == [first.log_path.name, second.log_path.name]


def test_background_surface_keeps_sort_order_for_two_spawns_inside_one_second(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stamp has second resolution, so a re-dispatch can land on the same
    one. It still must not overwrite, and the names must still sort into the
    order the attempts happened — hence a letter suffix and not `-2`, which
    would sort ahead of the unsuffixed first attempt."""
    monkeypatch.setattr(surfaces.time, "strftime", lambda fmt: "20260726-143512")

    first = surfaces.BackgroundSurface().spawn("agent-issue-92", ["true"], tmp_path)
    second = surfaces.BackgroundSurface().spawn("agent-issue-92", ["true"], tmp_path)

    assert first.log_path is not None and second.log_path is not None
    assert first.log_path.name == "agent-issue-92-20260726-143512.log"
    assert second.log_path.name == "agent-issue-92-20260726-143512a.log"
    assert sorted(p.name for p in (tmp_path / ".agent-runs").glob("*.log")) == [
        first.log_path.name,
        second.log_path.name,
    ]


def test_background_surface_log_names_are_a_discover_runs_signal(tmp_path: Path) -> None:
    """The per-attempt name must still be what `runs.discover_runs` counts as a
    log candidate — a scheme change that stopped matching would silently drop
    the signal rather than fail."""
    spawned = surfaces.BackgroundSurface().spawn("agent-issue-92", ["true"], tmp_path)

    assert spawned.log_path is not None
    match = runs._LOG_RE.match(spawned.log_path.name)
    assert match is not None and int(match.group(1)) == 92
    # ...and must not be picked up as one of the other per-issue artifacts.
    for pattern in (runs._FEEDBACK_RE, runs._OUTCOME_RE):
        assert pattern.match(spawned.log_path.name) is None


def test_background_surface_prunes_logs_past_the_artifact_ttl(tmp_path: Path) -> None:
    runs_dir = tmp_path / ".agent-runs"
    runs_dir.mkdir()
    stale = runs_dir / "agent-issue-1-20200101-000000.log"
    stale.write_text("last year's attempt")
    old = time.time() - runs.ARTIFACT_TTL_S - 1
    os.utime(stale, (old, old))
    recent = runs_dir / "agent-issue-2-20200101-000000.log"
    recent.write_text("this week's attempt")
    # Live state, not history: a halt nobody has answered must survive.
    feedback = runs_dir / "issue-3-feedback.md"
    feedback.write_text("findings")
    os.utime(feedback, (old, old))

    surfaces.BackgroundSurface().spawn("agent-issue-4", ["true"], tmp_path)

    assert not stale.exists()
    assert recent.exists()
    assert feedback.exists()


def _orca_spawn_calls(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        payload = {"result": {"terminal": {"handle": "term_abc"}}}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(surfaces, "run", fake_run)
    return calls


def test_orca_surface_attaches_terminal_to_attach_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _orca_spawn_calls(monkeypatch)
    spawned = surfaces.OrcaSurface().spawn(
        "agent-issue-7",
        ["agent", "implement", "7", "--project", "/repo"],
        Path("/repo"),
        attach_path=Path("/repo/.worktrees/issue-7"),
    )
    assert "term_abc" in spawned.where
    (cmd,) = calls
    assert cmd[1:3] == ["terminal", "create"]
    assert "path:/repo/.worktrees/issue-7" in cmd
    assert "agent implement 7 --project /repo" in cmd  # shell-joined, one --command arg


def test_orca_surface_defaults_attach_to_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _orca_spawn_calls(monkeypatch)
    surfaces.OrcaSurface().spawn(
        "agent-issue-7", ["agent", "implement", "7", "--project", "/repo"], Path("/repo")
    )
    (cmd,) = calls
    assert "path:/repo" in cmd


def _selector_not_found(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr='{"error":"selector_not_found"}')


def _worktree_selector(cmd: list[str]) -> str:
    return cmd[cmd.index("--worktree") + 1]


def test_orca_surface_retries_selector_not_found_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []
    responses = [
        _selector_not_found,
        lambda cmd: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"result": {"terminal": {"handle": "term_abc"}}}), stderr=""
        ),
    ]

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return responses[len(calls) - 1](cmd)

    monkeypatch.setattr(surfaces, "run", fake_run)
    monkeypatch.setattr(surfaces.time, "sleep", lambda s: sleeps.append(s))

    spawned = surfaces.OrcaSurface().spawn(
        "agent-issue-7",
        ["agent", "implement", "7", "--project", "/repo"],
        Path("/repo"),
        attach_path=Path("/repo/.worktrees/issue-7"),
    )

    assert spawned.handle == "term_abc"
    assert len(calls) == 2
    assert all(_worktree_selector(c) == "path:/repo/.worktrees/issue-7" for c in calls)
    assert sleeps == [surfaces._ATTACH_DELAY_S]


def test_orca_surface_falls_back_to_project_root_after_persistent_selector_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if _worktree_selector(cmd) == "path:/repo":
            payload = {"result": {"terminal": {"handle": "term_root"}}}
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
        return _selector_not_found(cmd)

    monkeypatch.setattr(surfaces, "run", fake_run)
    monkeypatch.setattr(surfaces.time, "sleep", lambda s: None)

    spawned = surfaces.OrcaSurface().spawn(
        "agent-issue-7",
        ["agent", "implement", "7"],
        Path("/repo"),
        attach_path=Path("/repo/.worktrees/issue-7"),
    )

    assert "term_root" in spawned.where
    assert "fell back" in spawned.where
    assert "/repo" in spawned.where  # states where the shell actually starts
    # the fallback terminal is still a terminal: its handle must be kept too,
    # or a run that landed on the project-root card would silently lose its
    # push channel
    assert spawned.handle == "term_root"
    assert len(calls) == surfaces._ATTACH_ATTEMPTS + 1
    assert _worktree_selector(calls[-1]) == "path:/repo"
    assert "project root" in calls[-1][calls[-1].index("--title") + 1]


def test_orca_surface_raises_when_worktree_and_fallback_both_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _selector_not_found(cmd)

    monkeypatch.setattr(surfaces, "run", fake_run)
    monkeypatch.setattr(surfaces.time, "sleep", lambda s: None)

    with pytest.raises(CommandError, match="selector_not_found"):
        surfaces.OrcaSurface().spawn(
            "agent-issue-7",
            ["agent", "implement", "7"],
            Path("/repo"),
            attach_path=Path("/repo/.worktrees/issue-7"),
        )


def test_orca_surface_raises_immediately_on_non_selector_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="permission_denied: nope")

    monkeypatch.setattr(surfaces, "run", fake_run)
    monkeypatch.setattr(surfaces.time, "sleep", lambda s: sleeps.append(s))

    with pytest.raises(CommandError, match="permission_denied"):
        surfaces.OrcaSurface().spawn(
            "agent-issue-7",
            ["agent", "implement", "7"],
            Path("/repo"),
            attach_path=Path("/repo/.worktrees/issue-7"),
        )

    assert len(calls) == 1
    assert sleeps == []


def _handle_timeout(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """What `orca terminal create` actually returned in issue #117.

    Note what it says: the handle timed out *after creation*. The terminal
    exists; only the answer was late."""
    body = {
        "ok": False,
        "error": {
            "code": "runtime_error",
            "message": "Timed out waiting for terminal handle after creation",
        },
    }
    return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps(body), stderr="")


def test_orca_surface_retries_a_transient_error_that_is_not_selector_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only `selector_not_found` was retryable, so the first real `agent spawn`
    aborted on a timeout that succeeded when re-run by hand (#117)."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if len(calls) == 1:
            return _handle_timeout(cmd)
        payload = {"result": {"terminal": {"handle": "term_abc"}}}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(surfaces, "run", fake_run)
    monkeypatch.setattr(surfaces.time, "sleep", lambda s: None)
    # Orca answers, and consistently: nothing was created by the failed attempt.
    monkeypatch.setattr(surfaces.orca, "terminal_handles", lambda path: set())

    spawned = surfaces.OrcaSurface().spawn(
        "agent-issue-7", ["agent", "implement", "7"], Path("/repo")
    )

    assert spawned.handle == "term_abc"
    assert len(calls) == 2


def test_orca_surface_adopts_the_terminal_a_timed_out_create_left_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole of #117's comment: the retry made a *second* live agent session
    on one worktree and one branch, and the spawn record named only one of them,
    so the other was invisible to `agent runs` and unaddressable by
    `messages.send_outcome`."""
    creates: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        creates.append(cmd)
        return _handle_timeout(cmd)

    listed = [set(), {"term_orphan"}]

    def fake_handles(path: Path) -> set[str]:
        return listed[min(len(creates), len(listed) - 1)]

    monkeypatch.setattr(surfaces, "run", fake_run)
    monkeypatch.setattr(surfaces.time, "sleep", lambda s: None)
    monkeypatch.setattr(surfaces.orca, "terminal_handles", fake_handles)

    spawned = surfaces.OrcaSurface().spawn(
        "agent-issue-7", ["agent", "implement", "7"], Path("/repo")
    )

    # One create, and the terminal it silently made is the one we run with.
    assert len(creates) == 1
    assert spawned.handle == "term_orphan"
    assert "adopted" in spawned.where


def test_orca_surface_adopts_nothing_when_orca_cannot_list_terminals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `None` baseline is "unknown", not "empty" — diffing against it would
    adopt whatever happened to be on the worktree, including somebody else's
    session."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _handle_timeout(cmd)

    monkeypatch.setattr(surfaces, "run", fake_run)
    monkeypatch.setattr(surfaces.time, "sleep", lambda s: None)
    monkeypatch.setattr(surfaces.orca, "terminal_handles", lambda path: None)

    with pytest.raises(CommandError, match="Timed out"):
        surfaces.OrcaSurface().spawn("agent-issue-7", ["agent", "implement", "7"], Path("/repo"))

    assert len(calls) == surfaces._ATTACH_ATTEMPTS


def test_orca_surface_no_duplicate_fallback_when_attach_path_is_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _selector_not_found(cmd)

    monkeypatch.setattr(surfaces, "run", fake_run)
    monkeypatch.setattr(surfaces.time, "sleep", lambda s: None)

    with pytest.raises(CommandError):
        surfaces.OrcaSurface().spawn("agent-issue-7", ["agent", "implement", "7"], Path("/repo"))

    assert len(calls) == surfaces._ATTACH_ATTEMPTS
    assert all(_worktree_selector(c) == "path:/repo" for c in calls)


def test_orca_surface_raises_command_error_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    monkeypatch.setattr(surfaces, "run", fake_run)

    with pytest.raises(CommandError):
        surfaces.OrcaSurface().spawn("agent-issue-7", ["agent", "implement", "7"], Path("/repo"))


def test_orca_surface_raises_command_error_on_handle_less_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"result": {}}), stderr="")

    monkeypatch.setattr(surfaces, "run", fake_run)

    with pytest.raises(CommandError):
        surfaces.OrcaSurface().spawn("agent-issue-7", ["agent", "implement", "7"], Path("/repo"))


def test_pick_auto_falls_back_to_background(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(surfaces.OrcaSurface, "available", lambda self: False)
    assert surfaces.pick("auto").name == "background"


def test_pick_unknown_surface_raises() -> None:
    with pytest.raises(ValueError, match="Unknown surface"):
        surfaces.pick("teleporter")


def test_pick_unavailable_surface_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(surfaces.OrcaSurface, "available", lambda self: False)
    with pytest.raises(ValueError, match="not available"):
        surfaces.pick("orca")


def test_background_surface_reports_its_identity_without_a_handle(tmp_path: Path) -> None:
    """Issue #98: the background surface satisfies the widened protocol by
    filling in what it has. No handle means no push channel, which is the
    permanent and expected state here and in the CI lane."""
    spawned = surfaces.BackgroundSurface().spawn("demo", ["true"], tmp_path)

    assert spawned.surface == "background"
    assert spawned.handle is None
    assert spawned.pid is not None
    assert spawned.log_path is not None
    assert spawned.log_path.exists()
    assert spawned.log_path.parent == tmp_path / ".agent-runs"
