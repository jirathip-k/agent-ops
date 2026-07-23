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


def test_pick_auto_falls_back_to_background(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(surfaces.HerdrSurface, "available", lambda self: False)
    assert surfaces.pick("auto").name == "background"


def test_pick_unknown_surface_raises() -> None:
    with pytest.raises(ValueError, match="Unknown surface"):
        surfaces.pick("teleporter")


def test_pick_unavailable_surface_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(surfaces.HerdrSurface, "available", lambda self: False)
    with pytest.raises(ValueError, match="not available"):
        surfaces.pick("herdr")
