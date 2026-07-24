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


def test_orca_surface_spawns_terminal_in_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        payload = {"result": {"terminal": {"handle": "term_abc"}}}
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(surfaces, "run", fake_run)
    where = surfaces.OrcaSurface().spawn(
        "agent-issue-7", ["agent", "implement", "7", "--project", "/repo"], Path("/repo")
    )
    assert "term_abc" in where
    (cmd,) = calls
    assert cmd[1:3] == ["terminal", "create"]
    assert "path:/repo" in cmd
    assert "agent implement 7 --project /repo" in cmd  # shell-joined, one --command arg


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
