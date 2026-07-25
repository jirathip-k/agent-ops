import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_ops import orca


def test_executable_honors_orca_cli_command_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCA_CLI_COMMAND", "orca-dev")
    assert orca.executable() == "orca-dev"


def test_report_updates_worktree_card(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", fake_run)
    orca.report(Path("/wt"), comment="planning", status=orca.STATUS_IN_PROGRESS)
    (cmd,) = calls
    assert cmd[1:3] == ["worktree", "set"]
    assert "path:/wt" in cmd
    assert "planning" in cmd
    assert "in-progress" in cmd


def test_report_is_a_noop_without_orca(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("must not invoke the CLI when Orca is unavailable")

    monkeypatch.setattr(orca, "available", lambda: False)
    monkeypatch.setattr(orca, "run", boom)
    orca.report(Path("/wt"), comment="planning")


def test_report_with_nothing_to_say_skips_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("must not invoke the CLI with no comment and no status")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", boom)
    orca.report(Path("/wt"))


def test_report_signals_whether_the_card_was_written(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(
        orca,
        "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="nope"),
    )
    assert orca.report(Path("/wt"), comment="hi") is False


def _worktree_selector(cmd: list[str]) -> str:
    return cmd[cmd.index("--worktree") + 1]


def test_report_indexed_card_makes_exactly_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", fake_run)

    ok = orca.report(Path("/wt"), comment="hi", fallback_path=Path("/repo"))

    assert ok is True
    assert len(calls) == 1
    assert _worktree_selector(calls[0]) == "path:/wt"


def test_report_falls_back_to_project_root_when_worktree_card_is_unindexed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if _worktree_selector(cmd) == "path:/repo":
            return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="selector_not_found")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", fake_run)

    ok = orca.report(Path("/wt"), comment="hi", fallback_path=Path("/repo"))

    assert ok is True
    assert len(calls) == 2
    assert _worktree_selector(calls[0]) == "path:/wt"
    assert _worktree_selector(calls[1]) == "path:/repo"


def test_report_returns_false_when_worktree_and_fallback_both_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="selector_not_found")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", fake_run)

    ok = orca.report(Path("/wt"), comment="hi", fallback_path=Path("/repo"))

    assert ok is False
    assert len(calls) == 2


def test_report_does_not_retry_without_a_fallback_path(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="selector_not_found")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", fake_run)

    assert orca.report(Path("/wt"), comment="hi") is False
    assert len(calls) == 1


def test_report_does_not_retry_a_non_selector_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="permission_denied")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", fake_run)

    ok = orca.report(Path("/wt"), comment="hi", fallback_path=Path("/repo"))

    assert ok is False
    assert len(calls) == 1


def test_await_indexed_returns_once_orca_sees_every_worktree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        # Orca's rescan finds the new checkout on the second poll.
        ok = len(calls) > 1
        payload = json.dumps({"ok": ok, "result": {"worktree": {}} if ok else {}})
        return subprocess.CompletedProcess(cmd, 0 if ok else 1, stdout=payload, stderr="")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", fake_run)
    monkeypatch.setattr(orca.time, "sleep", lambda s: sleeps.append(s))

    assert orca.await_indexed([Path("/wt")]) is True
    assert len(calls) == 2
    assert all(cmd[1:3] == ["worktree", "show"] for cmd in calls)
    assert sleeps == [orca._INDEX_DELAY_S]


def test_await_indexed_polls_only_the_paths_still_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polled: list[str] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        selector = cmd[cmd.index("--worktree") + 1]
        polled.append(selector)
        found = selector == "path:/a" or polled.count(selector) > 1
        payload = json.dumps({"ok": found, "result": {"worktree": {}} if found else {}})
        return subprocess.CompletedProcess(cmd, 0 if found else 1, stdout=payload, stderr="")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", fake_run)
    monkeypatch.setattr(orca.time, "sleep", lambda s: None)

    assert orca.await_indexed([Path("/a"), Path("/b")]) is True
    assert polled == ["path:/a", "path:/b", "path:/b"]  # /a is not re-polled


def test_await_indexed_gives_up_after_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="selector_not_found")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", fake_run)
    monkeypatch.setattr(orca.time, "sleep", lambda s: None)

    assert orca.await_indexed([Path("/wt")]) is False
    assert len(calls) == orca._INDEX_ATTEMPTS


def test_await_indexed_is_a_noop_without_paths_or_orca(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("must not poll with nothing to wait for")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", boom)
    assert orca.await_indexed([]) is False

    monkeypatch.setattr(orca, "available", lambda: False)
    assert orca.await_indexed([Path("/wt")]) is False


def _json_run(
    monkeypatch: pytest.MonkeyPatch, payload: object, returncode: int = 0
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(cmd, returncode, stdout=text, stderr="")

    monkeypatch.setattr(orca, "available", lambda: True)
    monkeypatch.setattr(orca, "run", fake_run)
    return calls


def _repo_entry(**over: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "repo-1",
        "path": "/Users/me/Projects/agent-ops",
        "gitRemoteIdentity": {"canonicalKey": "github.com/jirathip-k/agent-ops"},
    }
    entry.update(over)
    return entry


def test_repos_maps_orca_repos_to_github_slugs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _json_run(monkeypatch, {"ok": True, "result": {"repos": [_repo_entry()]}})

    (repo,) = orca.repos()

    assert repo.id == "repo-1"
    assert repo.path == Path("/Users/me/Projects/agent-ops")
    assert repo.slug == "jirathip-k/agent-ops"
    (cmd,) = calls
    assert cmd[1:3] == ["repo", "list"]
    assert "--json" in cmd


def test_repos_leaves_remoteless_repos_unslugged(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [_repo_entry(gitRemoteIdentity=None), _repo_entry(id="r2", gitRemoteIdentity={})]
    _json_run(monkeypatch, {"ok": True, "result": {"repos": entries}})
    assert [repo.slug for repo in orca.repos()] == [None, None]


def test_repos_skips_entries_missing_an_id_or_path(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [_repo_entry(), {"id": "r2"}, {"path": "/x"}, "junk"]
    _json_run(monkeypatch, {"ok": True, "result": {"repos": entries}})
    assert [repo.id for repo in orca.repos()] == ["repo-1"]


def test_repos_degrades_to_empty_on_a_failed_call(monkeypatch: pytest.MonkeyPatch) -> None:
    _json_run(monkeypatch, {"ok": False}, returncode=1)
    assert orca.repos() == []


def test_repos_degrades_to_empty_on_unparseable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _json_run(monkeypatch, "not json at all")
    assert orca.repos() == []


def test_repos_is_empty_without_orca(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("must not invoke the CLI when Orca is unavailable")

    monkeypatch.setattr(orca, "available", lambda: False)
    monkeypatch.setattr(orca, "run", boom)
    assert orca.repos() == []
    assert orca.cards("repo-1") == []


def test_cards_reads_worktrees_including_external_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [
        {
            "path": "/Users/me/Projects/agent-ops/.worktrees/issue-7",
            "branch": "refs/heads/fix/issue-7",
            "comment": "attempt 1/3",
            "workspaceStatus": "in-progress",
        },
        {"path": "/Users/me/Projects/agent-ops", "branch": "refs/heads/main", "comment": ""},
    ]
    calls = _json_run(monkeypatch, {"ok": True, "result": {"worktrees": entries}})

    card, root = orca.cards("repo-1")

    assert card.path == Path("/Users/me/Projects/agent-ops/.worktrees/issue-7")
    assert card.branch == "fix/issue-7"  # refs/heads/ stripped for branch lookups
    assert card.comment == "attempt 1/3"
    assert card.workspace_status == "in-progress"
    assert root.workspace_status is None  # no status set yet
    (cmd,) = calls
    assert cmd[1:3] == ["worktree", "list"]
    assert "id:repo-1" in cmd


def test_cards_skips_pathless_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    _json_run(monkeypatch, {"ok": True, "result": {"worktrees": [{"branch": "refs/heads/x"}]}})
    assert orca.cards("repo-1") == []
