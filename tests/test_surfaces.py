import json
import subprocess
import time
from pathlib import Path

import pytest

from agent_ops import surfaces
from agent_ops.utils import CommandError


def test_background_surface_spawns_and_logs(tmp_path: Path) -> None:
    where = surfaces.BackgroundSurface().spawn("demo", ["sh", "-c", "echo surface-works"], tmp_path)
    assert "background pid" in where
    log = tmp_path / ".agent-runs" / "demo.log"
    for _ in range(50):
        if log.exists() and "surface-works" in log.read_text():
            break
        time.sleep(0.05)
    assert "surface-works" in log.read_text()


def test_background_surface_logs_under_cwd_not_attach_path(tmp_path: Path) -> None:
    wt = tmp_path / ".worktrees" / "issue-1"
    wt.mkdir(parents=True)
    surfaces.BackgroundSurface().spawn("demo", ["true"], tmp_path, attach_path=wt)
    # the attach target may be deleted on success; the log must outlive it
    assert (tmp_path / ".agent-runs" / "demo.log").exists()
    assert not (wt / ".agent-runs").exists()


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
    where = surfaces.OrcaSurface().spawn(
        "agent-issue-7",
        ["agent", "implement", "7", "--project", "/repo"],
        Path("/repo"),
        attach_path=Path("/repo/.worktrees/issue-7"),
    )
    assert "term_abc" in where
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

    where = surfaces.OrcaSurface().spawn(
        "agent-issue-7",
        ["agent", "implement", "7", "--project", "/repo"],
        Path("/repo"),
        attach_path=Path("/repo/.worktrees/issue-7"),
    )

    assert "term_abc" in where
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

    where = surfaces.OrcaSurface().spawn(
        "agent-issue-7",
        ["agent", "implement", "7"],
        Path("/repo"),
        attach_path=Path("/repo/.worktrees/issue-7"),
    )

    assert "term_root" in where
    assert "fell back" in where
    assert "/repo" in where  # states where the shell actually starts
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
