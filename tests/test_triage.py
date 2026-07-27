import json
import re
from pathlib import Path
from typing import Any

import pytest

from agent_ops import github, worktree
from agent_ops.config import ProjectConfig
from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult
from agent_ops.utils import PLATFORM_ROOT, CommandError
from agent_ops.workflows import triage as triage_module
from agent_ops.workflows.triage import GATE_LABELS, parse_triage, run_triage


def _description_created_in(workflow: str, label: str) -> str:
    """The `--description` a workflow YAML passes to `gh label create <label>`."""
    text = (PLATFORM_ROOT / ".github" / "workflows" / workflow).read_text()
    match = re.search(rf'gh label create {label}.*?--description "([^"]+)"', text, re.DOTALL)
    assert match, f"no `gh label create {label}` with --description found in {workflow}"
    return match.group(1)


def test_gate_label_descriptions_match_what_the_workflow_yamls_create() -> None:
    """spec-pipeline.yml / plan-pipeline.yml each create their own gate label
    (so it exists even before `agent init` or groom ever runs), with its own
    hardcoded `--description --force`. If that text drifts from GATE_LABELS,
    the two writers fight over the label's description on every run — pinning
    them here means the next wording tweak can't silently reopen that."""
    assert GATE_LABELS["spec-requested"].description == _description_created_in(
        "spec-pipeline.yml", "spec-requested"
    )
    assert GATE_LABELS["plan-requested"].description == _description_created_in(
        "plan-pipeline.yml", "plan-requested"
    )


def test_gate_label_descriptions_do_not_attribute_the_request_to_a_human() -> None:
    """groom can emit these labels itself (#97); the description must not claim
    a human always applied them."""
    for name, label in GATE_LABELS.items():
        assert "human" not in label.description.lower(), name


def test_parses_result_block() -> None:
    text = """I explored the code. Here are my conclusions.

TRIAGE RESULTS:
#12 agent-ready — clear repro, fix is localized to one component
#13 needs-human — requires a product decision on data retention
#14 backlog — idea without acceptance criteria
"""
    results = parse_triage(text)
    assert [(r.number, r.verdict) for r in results] == [
        (12, "agent-ready"),
        (13, "needs-human"),
        (14, "backlog"),
    ]
    assert results[0].reason.startswith("clear repro")


def test_uses_last_marker_and_ignores_junk_lines() -> None:
    text = (
        "TRIAGE RESULTS:\n#1 backlog — early draft\n"
        "TRIAGE RESULTS:\n#2 agent-ready — final\nnot a result line\n"
    )
    results = parse_triage(text)
    assert [(r.number, r.verdict) for r in results] == [(2, "agent-ready")]


def test_no_marker_returns_empty() -> None:
    assert parse_triage("no block here") == []


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


def _issue(number: int) -> dict[str, Any]:
    return {"number": number, "title": f"issue {number}", "body": "some body", "labels": []}


def _stub_triage_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verdict_block: str,
    issues: list[dict[str, Any]],
) -> list[list[str]]:
    """Drive run_triage against fake issues; return every `gh`/`git` argv it ran."""
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

    monkeypatch.setattr(triage_module, "run", fake_run)
    monkeypatch.setattr(triage_module, "role_request", fake_role_request)
    monkeypatch.setattr(worktree, "create_detached", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(worktree, "remove", lambda *args, **kwargs: None)
    return calls


def test_run_triage_syncs_labels_before_applying_verdicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_triage_run(
        monkeypatch,
        tmp_path,
        "TRIAGE RESULTS:\n#12 agent-ready — clear repro\n",
        [_issue(12)],
    )
    synced: list[dict[str, github.Label]] = []
    seen_repo: list[str | None] = []

    def fake_sync_labels(
        project_root: Path, labels: dict[str, github.Label], *, repo: str | None = None
    ) -> github.LabelSync:
        synced.append(labels)
        seen_repo.append(repo)
        return github.LabelSync(created=list(labels), updated=[], unchanged=[], failed=[])

    monkeypatch.setattr(github, "sync_labels", fake_sync_labels)
    monkeypatch.setattr(github, "remote_slug", lambda root: "acme/widgets")

    results = run_triage(tmp_path, log=lambda _msg: None)

    assert [(r.number, r.verdict) for r in results] == [(12, "agent-ready")]
    assert synced and "agent-ready" in synced[0]
    # triage pins the repo it resolved, so a fork's `gh` base-repo resolution
    # can't redirect the sync to the wrong repository (the same guard `init`
    # has — dropping this from any lane reintroduces the fork redirect).
    assert seen_repo == ["acme/widgets"]


def test_run_triage_survives_a_label_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_triage_run(
        monkeypatch,
        tmp_path,
        "TRIAGE RESULTS:\n#12 agent-ready — clear repro\n",
        [_issue(12)],
    )

    def fake_sync_labels(
        project_root: Path, labels: dict[str, github.Label], *, repo: str | None = None
    ) -> github.LabelSync:
        raise CommandError("no write scope")

    monkeypatch.setattr(github, "sync_labels", fake_sync_labels)
    logged: list[str] = []

    results = run_triage(tmp_path, log=logged.append)

    assert [(r.number, r.verdict) for r in results] == [(12, "agent-ready")]
    assert any("could not sync labels" in line for line in logged)


def test_run_triage_survives_gh_not_being_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`utils.run` raises FileNotFoundError, not CommandError, when `gh` isn't on
    PATH — a missing `gh` here must not abort a triage run after the agent has
    already produced results."""
    _stub_triage_run(
        monkeypatch,
        tmp_path,
        "TRIAGE RESULTS:\n#12 agent-ready — clear repro\n",
        [_issue(12)],
    )

    def missing_gh(
        project_root: Path, labels: dict[str, github.Label], *, repo: str | None = None
    ) -> github.LabelSync:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(github, "sync_labels", missing_gh)
    logged: list[str] = []

    results = run_triage(tmp_path, log=logged.append)

    assert [(r.number, r.verdict) for r in results] == [(12, "agent-ready")]
    assert any("could not sync labels" in line for line in logged)


def test_run_triage_logs_a_single_label_failure_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_triage_run(
        monkeypatch,
        tmp_path,
        "TRIAGE RESULTS:\n#12 agent-ready — clear repro\n",
        [_issue(12)],
    )

    def fake_sync_labels(
        project_root: Path, labels: dict[str, github.Label], *, repo: str | None = None
    ) -> github.LabelSync:
        return github.LabelSync(
            created=[], updated=[], unchanged=[], failed=[("agent-ready", "HTTP 403: no scope")]
        )

    monkeypatch.setattr(github, "sync_labels", fake_sync_labels)
    logged: list[str] = []

    results = run_triage(tmp_path, log=logged.append)

    assert [(r.number, r.verdict) for r in results] == [(12, "agent-ready")]
    assert any("agent-ready" in line and "HTTP 403: no scope" in line for line in logged)
