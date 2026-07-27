import subprocess
import sys
import uuid
from typing import Any

import pytest

from agent_ops.utils import (
    DEFAULT_TIMEOUT_S,
    SLOW_GIT_TIMEOUT_S,
    SPAWN_FAILURE_RETURNCODE,
    TIMEOUT_RETURNCODE,
    CommandError,
    run,
)


def test_overrunning_command_raises_naming_command_and_bound() -> None:
    with pytest.raises(CommandError) as excinfo:
        run(["sh", "-c", "sleep 5"], timeout=0.1)
    message = str(excinfo.value)
    assert "sh -c sleep 5" in message
    assert "0.1s" in message


def test_a_missing_binary_raises_command_error_not_a_raw_os_error() -> None:
    """A missing binary used to escape as a raw `FileNotFoundError` regardless
    of `check` (agent-ops#154) — under `check=True` it must now come back as
    the same `CommandError` a non-zero exit or a timeout would."""
    missing = f"agent-ops-nonexistent-binary-{uuid.uuid4().hex}"
    with pytest.raises(CommandError) as excinfo:
        run([missing])
    assert not isinstance(excinfo.value, FileNotFoundError)
    assert missing in str(excinfo.value)


def test_timeout_under_check_false_never_raises() -> None:
    """`check=False` reads as "do not raise" — a timeout must not be the one
    failure mode it forgets to cover (agent-ops#154)."""
    proc = run(["sh", "-c", "sleep 5"], check=False, timeout=0.1)
    assert proc.returncode == TIMEOUT_RETURNCODE
    assert proc.stdout == ""
    assert "0.1s" in proc.stderr


def test_missing_binary_under_check_false_never_raises() -> None:
    """A missing binary must surface the same way a timeout or non-zero exit
    does under `check=False`: as a returncode to inspect, not a traceback."""
    missing = f"agent-ops-nonexistent-binary-{uuid.uuid4().hex}"
    proc = run([missing], check=False)
    assert proc.returncode == SPAWN_FAILURE_RETURNCODE
    assert proc.stdout == ""
    assert missing in proc.stderr


def test_non_zero_exit_is_unaffected_by_check() -> None:
    checked = run([sys.executable, "-c", "raise SystemExit(3)"], check=False)
    assert checked.returncode == 3

    with pytest.raises(CommandError):
        run([sys.executable, "-c", "raise SystemExit(3)"])


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
