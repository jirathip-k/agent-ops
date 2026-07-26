from __future__ import annotations

import pytest

from agent_ops import messages, orca


@pytest.fixture(autouse=True)
def _orca_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Orca is off unless a test opts in, and no ambient terminal handle leaks in.

    Every Orca-touching path is written to no-op when `orca.available()` is
    False, so this is the state CI runs in. Without it the suite behaves
    differently depending on whether the developer happens to be inside the
    Orca IDE — and `messages.wait_for_message` in particular would then block
    on a real `orca orchestration check --wait` for the poll interval of every
    `wait_for_runs` test.

    `ORCA_TERMINAL_HANDLE` goes for the same reason: it is set in every Orca
    terminal, and `messages` falls back to it when there is no spawn record,
    so leaving it in place would make "this run has no push channel" tests
    pass or fail depending on where the suite was started from.

    Tests that want the Orca paths already patch `available` to True
    themselves; an explicit `monkeypatch.setattr` inside the test overrides
    this one.
    """
    monkeypatch.setattr(orca, "available", lambda: False)
    monkeypatch.delenv(messages._HANDLE_ENV, raising=False)
