import json
import subprocess
from pathlib import Path

import pytest

from agent_ops import messages, orca
from agent_ops.utils import CommandError


def _orca_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "executable", lambda: "orca")


def _fake_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: str = "",
    returncode: int = 0,
) -> list[list[str]]:
    """Record every argv `messages` shells out with; reply with one canned result."""
    calls: list[list[str]] = []

    def fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="boom")

    monkeypatch.setattr(messages, "run", fake)
    return calls


def _check_payload(*, issue: int, state: str, pr_url: str | None = None) -> str:
    body = {"issue": issue, "state": state, "pr_url": pr_url}
    return json.dumps(
        {"result": {"messages": [{"type": "worker_done", "payload": json.dumps(body)}]}}
    )


def _value(cmd: list[str], flag: str) -> str:
    return cmd[cmd.index(flag) + 1]


# --- the spawn record ------------------------------------------------------


def test_record_and_load_spawn_round_trips_the_handle(tmp_path: Path) -> None:
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    record = messages.load_spawn(tmp_path, 98)
    assert record is not None
    assert record.handle == "term_abc"
    assert record.surface == "orca"
    assert record.issue == 98


def test_record_spawn_keeps_the_background_surfaces_identity(tmp_path: Path) -> None:
    """No handle is not an error — it is the background surface's normal shape."""
    log_path = tmp_path / ".agent-runs" / "agent-issue-98.log"
    messages.record_spawn(tmp_path, 98, surface="background", pid=4242, log_path=log_path)
    record = messages.load_spawn(tmp_path, 98)
    assert record is not None
    assert record.handle is None
    assert record.pid == 4242
    assert record.log_path == str(log_path)


def test_record_spawn_overwrites_so_the_mailbox_follows_the_latest_cycle(tmp_path: Path) -> None:
    """A second dispatch owns the issue; the first cycle's handle must not linger."""
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_first")
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_second")
    record = messages.load_spawn(tmp_path, 98)
    assert record is not None
    assert record.handle == "term_second"


def test_load_spawn_is_silent_when_there_is_no_record(tmp_path: Path) -> None:
    logged: list[str] = []
    assert messages.load_spawn(tmp_path, 98, log=logged.append) is None
    assert logged == []  # the ordinary case for any run dispatched before #98


def test_load_spawn_warns_and_degrades_on_a_corrupt_record(tmp_path: Path) -> None:
    path = messages.spawn_path(tmp_path, 98)
    path.parent.mkdir(exist_ok=True)
    path.write_text("{ not json")
    logged: list[str] = []
    assert messages.load_spawn(tmp_path, 98, log=logged.append) is None
    assert any("spawn record" in line for line in logged)


def test_spawn_record_is_not_one_of_discover_runs_signal_patterns(tmp_path: Path) -> None:
    """An address book is not evidence a run exists — see `runs.discover_runs`."""
    from agent_ops import runs

    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    name = messages.spawn_path(tmp_path, 98).name
    for pattern in (runs._FEEDBACK_RE, runs._LOG_RE, runs._OUTCOME_RE):
        assert pattern.match(name) is None


# --- sending ---------------------------------------------------------------


def test_send_outcome_is_a_no_op_without_orca(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    calls = _fake_run(monkeypatch)  # conftest leaves orca.available() False
    assert messages.send_outcome(tmp_path, 98, state="done") is False
    assert calls == []


def test_send_outcome_is_a_no_op_without_a_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The background surface and the CI lane never have one; that is not a failure."""
    _orca_on(monkeypatch)
    messages.record_spawn(tmp_path, 98, surface="background", pid=1)
    calls = _fake_run(monkeypatch)
    assert messages.send_outcome(tmp_path, 98, state="done") is False
    assert calls == []


def test_send_outcome_payload_matches_the_durable_outcome_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One schema with the issue number added, not a parallel one (issue #87/#98)."""
    from agent_ops.workflows import implement as implement_module

    _orca_on(monkeypatch)
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    calls = _fake_run(monkeypatch, stdout="{}")

    assert messages.send_outcome(tmp_path, 98, state="done", pr_url="https://x/pull/99") is True

    payload = json.loads(_value(calls[0], "--payload"))
    implement_module._write_outcome(
        tmp_path, 98, state="done", pr_url="https://x/pull/99", reason=None, log=lambda _: None
    )
    durable = json.loads(implement_module._outcome_path(tmp_path, 98).read_text())
    assert set(payload) == set(durable) | {"issue"}
    assert payload["issue"] == 98
    for field in ("state", "pr_url", "reason"):
        assert payload[field] == durable[field]


def test_send_outcome_addresses_the_recorded_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _orca_on(monkeypatch)
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    calls = _fake_run(monkeypatch, stdout="{}")

    messages.send_outcome(tmp_path, 98, state="done")

    (cmd,) = calls
    assert cmd[1:3] == ["orchestration", "send"]
    assert _value(cmd, "--to") == "term_abc"
    assert _value(cmd, "--from") == "term_abc"


def test_send_outcome_prefers_the_record_over_the_ambient_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record's handle is the one the supervisor watches; an inherited
    `ORCA_TERMINAL_HANDLE` (e.g. a background child of an Orca shell) is not."""
    _orca_on(monkeypatch)
    monkeypatch.setenv(messages._HANDLE_ENV, "term_ambient")
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_recorded")
    calls = _fake_run(monkeypatch, stdout="{}")

    messages.send_outcome(tmp_path, 98, state="done")

    assert _value(calls[0], "--to") == "term_recorded"


def test_send_outcome_falls_back_to_the_ambient_terminal_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _orca_on(monkeypatch)
    monkeypatch.setenv(messages._HANDLE_ENV, "term_ambient")
    calls = _fake_run(monkeypatch, stdout="{}")

    assert messages.send_outcome(tmp_path, 98, state="done") is True
    assert _value(calls[0], "--to") == "term_ambient"


@pytest.mark.parametrize(
    ("state", "kind"),
    [("done", "worker_done"), ("failed", "worker_done"), ("halted", "escalation")],
)
def test_send_outcome_escalates_only_the_state_a_human_must_act_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, kind: str
) -> None:
    _orca_on(monkeypatch)
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    calls = _fake_run(monkeypatch, stdout="{}")

    messages.send_outcome(tmp_path, 98, state=state)

    assert _value(calls[0], "--type") == kind


def test_send_outcome_reports_a_failed_send_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Best-effort: a dropped notification must never turn a finished run into a crash."""
    _orca_on(monkeypatch)
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    _fake_run(monkeypatch, returncode=1)
    logged: list[str] = []

    assert messages.send_outcome(tmp_path, 98, state="done", log=logged.append) is False
    assert any("could not push" in line for line in logged)


# --- collecting ------------------------------------------------------------


def test_collect_parses_a_reported_outcome(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _orca_on(monkeypatch)
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    calls = _fake_run(
        monkeypatch, stdout=_check_payload(issue=98, state="done", pr_url="https://x/pull/99")
    )

    found = messages.collect(tmp_path, [98])

    assert found[98].state == "done"
    assert found[98].pr_url == "https://x/pull/99"
    # `--unread`, not `--peek`: this is the consuming read
    assert "--unread" in calls[0]
    assert "--wait" not in calls[0]


def test_collect_drops_a_message_addressed_to_a_different_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _orca_on(monkeypatch)
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    _fake_run(monkeypatch, stdout=_check_payload(issue=77, state="done"))

    assert messages.collect(tmp_path, [98]) == {}


def test_collect_drops_an_unparseable_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _orca_on(monkeypatch)
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    stdout = json.dumps({"result": {"messages": [{"type": "worker_done", "payload": "{oops"}]}})
    _fake_run(monkeypatch, stdout=stdout)

    assert messages.collect(tmp_path, [98]) == {}


def test_collect_is_a_no_op_without_orca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    calls = _fake_run(monkeypatch, stdout=_check_payload(issue=98, state="done"))
    assert messages.collect(tmp_path, [98]) == {}
    assert calls == []


def test_collect_skips_issues_with_no_recorded_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _orca_on(monkeypatch)
    calls = _fake_run(monkeypatch, stdout=_check_payload(issue=98, state="done"))
    assert messages.collect(tmp_path, [98]) == {}
    assert calls == []


def test_collect_survives_a_failing_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _orca_on(monkeypatch)
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    _fake_run(monkeypatch, returncode=1)
    logged: list[str] = []

    assert messages.collect(tmp_path, [98], log=logged.append) == {}
    assert any("could not check messages" in line for line in logged)


# --- the blocking wait -----------------------------------------------------


def test_wait_for_message_peeks_so_the_message_survives_for_collect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _orca_on(monkeypatch)
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")
    calls = _fake_run(monkeypatch, stdout=_check_payload(issue=98, state="done"))

    assert messages.wait_for_message(tmp_path, 98, 15.0) is True

    (cmd,) = calls
    assert "--peek" in cmd
    assert "--wait" in cmd
    assert _value(cmd, "--timeout-ms") == "15000"
    assert _value(cmd, "--types") == "worker_done,escalation"


def test_wait_for_message_returns_false_for_a_stale_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Orca answers an unknown handle with an empty list, not an error — which
    is exactly why a stale record costs one poll interval and nothing more."""
    _orca_on(monkeypatch)
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_forgotten")
    _fake_run(monkeypatch, stdout=json.dumps({"result": {"messages": [], "count": 0}}))

    assert messages.wait_for_message(tmp_path, 98, 15.0) is False


def test_wait_for_message_returns_false_without_a_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _orca_on(monkeypatch)
    calls = _fake_run(monkeypatch, stdout="{}")
    assert messages.wait_for_message(tmp_path, 98, 15.0) is False
    assert calls == []


def test_wait_for_message_returns_false_when_the_cli_wedges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`utils.run`'s bound fires; a wedged helper must not wedge the wait."""
    _orca_on(monkeypatch)
    messages.record_spawn(tmp_path, 98, surface="orca", handle="term_abc")

    def hangs(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise CommandError("`orca` did not finish within 25s")

    monkeypatch.setattr(messages, "run", hangs)
    logged: list[str] = []

    assert messages.wait_for_message(tmp_path, 98, 15.0, log=logged.append) is False
    assert any("did not finish" in line for line in logged)
