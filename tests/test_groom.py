import json
from pathlib import Path
from typing import Any

import pytest

from agent_ops import worktree
from agent_ops.config import ProjectConfig
from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult
from agent_ops.workflows import groom as groom_module
from agent_ops.workflows.groom import VERDICTS, parse_groom, run_groom


def test_parses_all_verdicts() -> None:
    text = """Verified each issue against the working branch.

GROOM RESULTS:
#12 close-fixed — fix present in src/lib/recordingQueue.ts (commit bee4873, PR #120)
#13 close-invalid — duplicate of #12
#14 agent-ready — clear repro; acceptance: chart shows decimal RPE unrounded
#15 needs-human — requires a data-retention decision
#16 backlog — idea without acceptance criteria
#17 keep — already agent-ready and still valid
#18 spec-requested — one-line idea, needs acceptance criteria before planning
#19 plan-requested — real bug, but the fix spans the sync layer and needs a design
"""
    results = parse_groom(text)
    assert [(r.number, r.verdict) for r in results] == [
        (12, "close-fixed"),
        (13, "close-invalid"),
        (14, "agent-ready"),
        (15, "needs-human"),
        (16, "backlog"),
        (17, "keep"),
        (18, "spec-requested"),
        (19, "plan-requested"),
    ]
    assert results[0].reason.startswith("fix present")
    assert {r.verdict for r in results} <= VERDICTS


def test_unrecognised_verdict_parses_but_is_not_a_known_verdict() -> None:
    """An invented verdict must survive parsing so it can be logged, not vanish.

    A whitelist regex dropped the line, and a run whose every line was
    dropped raised "no parseable results" — a crash indistinguishable from a
    genuinely malformed run.
    """
    results = parse_groom("GROOM RESULTS:\n#21 wontfix — not doing this\n")

    assert [(r.number, r.verdict) for r in results] == [(21, "wontfix")]
    assert "wontfix" not in VERDICTS


def test_uses_last_marker_and_ignores_junk() -> None:
    text = "GROOM RESULTS:\n#1 keep — draft\nGROOM RESULTS:\n#2 close-fixed — final\nnoise\n"
    assert [(r.number, r.verdict) for r in parse_groom(text)] == [(2, "close-fixed")]


def test_no_marker_returns_empty() -> None:
    assert parse_groom("no block here") == []


class _FakeRuntime:
    name = "fake"

    def __init__(self, text: str) -> None:
        self.text = text

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest) -> RunResult:
        return RunResult(ok=True, text=self.text)

    def classify_failure(self, result: RunResult) -> FailureKind:
        return FailureKind.AGENT_FAILURE


class _FakeProc:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def _stub_groom_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verdict_block: str,
    issues: list[dict[str, Any]],
) -> list[list[str]]:
    """Drive run_groom against fake issues; return every `gh`/`git` argv it ran."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
        calls.append(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            return _FakeProc(json.dumps(issues))
        return _FakeProc("")

    def fake_role_request(
        config: ProjectConfig, role_name: str, prompt: str, cwd: Path, **kwargs: object
    ) -> tuple[object, RunRequest]:
        return _FakeRuntime(verdict_block), RunRequest(prompt=prompt, cwd=cwd)

    monkeypatch.setattr(groom_module, "run", fake_run)
    monkeypatch.setattr(groom_module, "role_request", fake_role_request)
    monkeypatch.setattr(worktree, "create_detached", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(worktree, "remove", lambda *args, **kwargs: None)
    return calls


def _issue(number: int, labels: list[str]) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"issue {number}",
        "body": "some body",
        "labels": [{"name": name} for name in labels],
        "updatedAt": "2026-07-25T00:00:00Z",
    }


def _edit_for(calls: list[list[str]], number: int) -> list[str] | None:
    for cmd in calls:
        if cmd[:4] == ["gh", "issue", "edit", str(number)]:
            return cmd
    return None


def test_gate_verdicts_are_applied_and_create_their_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_groom_run(
        monkeypatch,
        tmp_path,
        "GROOM RESULTS:\n"
        "#18 spec-requested — one-line idea, needs acceptance criteria\n"
        "#19 plan-requested — real bug, but the fix needs a design\n",
        [_issue(18, ["backlog"]), _issue(19, [])],
    )

    results = run_groom(tmp_path, log=lambda _msg: None)

    assert [(r.number, r.verdict) for r in results] == [
        (18, "spec-requested"),
        (19, "plan-requested"),
    ]
    assert _edit_for(calls, 18) == ["gh", "issue", "edit", "18", "--add-label", "spec-requested"]
    assert _edit_for(calls, 19) == ["gh", "issue", "edit", "19", "--add-label", "plan-requested"]
    # A label the repo never created makes `gh issue edit` fail outright, so
    # groom has to create the gate labels the same way it creates its own.
    created = {cmd[3] for cmd in calls if cmd[:3] == ["gh", "label", "create"]}
    assert {"spec-requested", "plan-requested"} <= created


def test_a_gate_verdict_clears_agent_ready_but_keeps_the_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Routing to a lane means "not ready to implement" — but the issue keeps
    its bucket, or the next triage run would re-classify it from scratch."""
    calls = _stub_groom_run(
        monkeypatch,
        tmp_path,
        "GROOM RESULTS:\n#20 plan-requested — promoted too early, needs a design\n",
        [_issue(20, ["agent-ready", "backlog"])],
    )

    run_groom(tmp_path, log=lambda _msg: None)

    edit = _edit_for(calls, 20)
    assert edit is not None
    assert edit[4:6] == ["--add-label", "plan-requested"]
    assert "agent-ready" in edit
    assert "backlog" not in edit


def test_a_bucket_verdict_still_strips_every_other_bucket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_groom_run(
        monkeypatch,
        tmp_path,
        "GROOM RESULTS:\n#22 agent-ready — root cause confirmed in the sync layer\n",
        [_issue(22, ["backlog", "needs-human"])],
    )

    run_groom(tmp_path, log=lambda _msg: None)

    edit = _edit_for(calls, 22)
    assert edit is not None
    assert sorted(edit[6:]) == sorted(
        ["--remove-label", "--remove-label", "backlog", "needs-human"]
    )


def test_an_unrecognised_verdict_is_a_logged_no_op_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verdict nobody defined must not reach `gh` — and must not raise.

    Left to the old whitelist regex the line was dropped instead, and a run
    where every line was dropped raised "no parseable results": a crash on
    the agent's wording rather than a skip.
    """
    calls = _stub_groom_run(
        monkeypatch,
        tmp_path,
        "GROOM RESULTS:\n#23 wontfix — not doing this\n#24 keep — still valid\n",
        [_issue(23, []), _issue(24, ["agent-ready"])],
    )
    logged: list[str] = []

    results = run_groom(tmp_path, log=logged.append)

    assert [(r.number, r.verdict) for r in results] == [(23, "wontfix"), (24, "keep")]
    assert _edit_for(calls, 23) is None
    assert not any(cmd[:3] == ["gh", "issue", "close"] for cmd in calls)
    assert not any(cmd[:3] == ["gh", "issue", "comment"] for cmd in calls)
    assert any("unrecognised verdict 'wontfix'" in line for line in logged)


def test_a_run_of_only_unrecognised_verdicts_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_groom_run(
        monkeypatch,
        tmp_path,
        "GROOM RESULTS:\n#25 maybe-later — unsure\n",
        [_issue(25, [])],
    )

    assert [r.verdict for r in run_groom(tmp_path, log=lambda _msg: None)] == ["maybe-later"]
