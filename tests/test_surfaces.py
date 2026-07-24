import json
import subprocess
import time
from pathlib import Path

import pytest

from agent_ops import surfaces


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
