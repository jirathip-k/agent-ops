import subprocess
from typing import Any

import pytest

from agent_ops.utils import DEFAULT_TIMEOUT_S, SLOW_GIT_TIMEOUT_S, CommandError, run


def test_overrunning_command_raises_naming_command_and_bound() -> None:
    with pytest.raises(CommandError) as excinfo:
        run(["sh", "-c", "sleep 5"], timeout=0.1)
    message = str(excinfo.value)
    assert "sh -c sleep 5" in message
    assert "0.1s" in message


def test_timeout_fires_even_when_check_is_false() -> None:
    """A timeout leaves no exit code to inspect, so `check=False` cannot swallow it."""
    with pytest.raises(CommandError):
        run(["sh", "-c", "sleep 5"], check=False, timeout=0.1)


def test_short_default_applies_without_an_explicit_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}
    real = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    run(["true"])
    assert seen["timeout"] == DEFAULT_TIMEOUT_S


def test_explicit_none_opts_out_of_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long-running children (the runtime adapters) must stay unbounded."""
    seen: dict[str, Any] = {}
    real = subprocess.run

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    run(["true"], timeout=None)
    assert seen["timeout"] is None


def test_slow_git_bound_is_longer_than_the_default() -> None:
    assert SLOW_GIT_TIMEOUT_S > DEFAULT_TIMEOUT_S
