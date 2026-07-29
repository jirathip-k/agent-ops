import json
import re
from pathlib import Path
from typing import Any

import pytest

from agent_ops import claims, github, worktree
from agent_ops.config import ProjectConfig
from agent_ops.prompts import render_task
from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult
from agent_ops.utils import PLATFORM_ROOT, CommandError
from agent_ops.workflows import triage as triage_module
from agent_ops.workflows.triage import (
    BUCKET_LABELS,
    GATE_LABELS,
    TRIAGE_DONE_LABEL,
    parse_triage,
    run_triage,
)


def _label_created_in(workflow: str, label: str) -> tuple[str, str]:
    """The `--color` and `--description` a workflow YAML passes to `gh label
    create <label>`."""
    text = (PLATFORM_ROOT / ".github" / "workflows" / workflow).read_text()
    match = re.search(
        rf'gh label create {label}.*?--color (\S+).*?--description "([^"]+)"', text, re.DOTALL
    )
    assert match, f"no `gh label create {label}` with --color/--description found in {workflow}"
    return match.group(1), match.group(2)


def test_gate_label_descriptions_match_what_the_workflow_yamls_create() -> None:
    """spec-pipeline.yml / plan-pipeline.yml each create their own gate label
    (so it exists even before `agent init` or groom ever runs), with its own
    hardcoded `--color --description --force`. If that text drifts from
    GATE_LABELS, the two writers fight over the label on every run — pinning
    both here means the next tweak (wording or color) can't silently reopen
    that (sync_labels compares color case-insensitively/`#`-stripped, so the
    color check here mirrors that)."""
    spec_color, spec_description = _label_created_in("spec-pipeline.yml", "spec-requested")
    assert GATE_LABELS["spec-requested"].description == spec_description
    assert GATE_LABELS["spec-requested"].color.lstrip("#").lower() == spec_color.lstrip("#").lower()

    plan_color, plan_description = _label_created_in("plan-pipeline.yml", "plan-requested")
    assert GATE_LABELS["plan-requested"].description == plan_description
    assert GATE_LABELS["plan-requested"].color.lstrip("#").lower() == plan_color.lstrip("#").lower()


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


def test_parses_a_reason_naming_bug_priority() -> None:
    """#257: bug priority (P0/P1/P2) lives in the reasoning, not a fourth
    bucket -- the reason line must still parse when it names one."""
    text = "TRIAGE RESULTS:\n#9 agent-ready — P0: production is down, fix is a one-line revert\n"
    results = parse_triage(text)
    assert [(r.number, r.verdict) for r in results] == [(9, "agent-ready")]
    assert results[0].reason.startswith("P0:")


def test_triage_prompt_covers_every_behaviour_moved_from_the_orchestrator() -> None:
    """#257: prompts/tasks/triage.md is now the single definition of
    classification, so the behaviours that used to live only in
    prompts/orchestrator.md Step 1 must render here. Brace-safety (`.format()`
    doesn't choke on a stray `{`/`}`) is covered by `test_task_templates_render`
    in test_prompts.py; this pins the content itself."""
    rendered = render_task("triage", issues="### #1: t")
    for marker in (
        "agent:claimed",
        "gh api repos/<owner>/<repo>/issues/<N>/events",
        "production down",
        "duplicate",
        "triage:done",
    ):
        assert marker in rendered, marker


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
    """A missing `gh` binary surfaces from `sync_labels` as `CommandError` —
    `utils.run` converts the underlying `FileNotFoundError` under `check=True`
    (agent-ops#154) — a missing `gh` here must not abort a triage run after
    the agent has already produced results."""
    _stub_triage_run(
        monkeypatch,
        tmp_path,
        "TRIAGE RESULTS:\n#12 agent-ready — clear repro\n",
        [_issue(12)],
    )

    def missing_gh(
        project_root: Path, labels: dict[str, github.Label], *, repo: str | None = None
    ) -> github.LabelSync:
        raise CommandError("could not list labels: `gh` could not be started")

    monkeypatch.setattr(github, "sync_labels", missing_gh)
    logged: list[str] = []

    results = run_triage(tmp_path, log=logged.append)

    assert [(r.number, r.verdict) for r in results] == [(12, "agent-ready")]
    assert any("could not sync labels" in line for line in logged)


def test_run_triage_skips_issues_settled_with_a_bucket_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#257 follow-up: settled means carrying any bucket label, `triage:done`
    or not -- an issue carrying a bucket alongside the stamp is still not
    sent to the prompt again."""
    prompts: list[str] = []
    monkeypatch.setattr(
        triage_module,
        "run",
        lambda cmd, **kwargs: (
            _FakeProc(
                json.dumps(
                    [
                        _issue(12),
                        {
                            **_issue(13),
                            "labels": [{"name": TRIAGE_DONE_LABEL}, {"name": "backlog"}],
                        },
                    ]
                )
            )
            if cmd[:3] == ["gh", "issue", "list"]
            else _FakeProc("")
        ),
    )

    def fake_role_request(
        config: ProjectConfig, role_name: str, prompt: str, cwd: Path, **kwargs: object
    ) -> tuple[object, RunRequest]:
        prompts.append(prompt)
        return _FakeRuntime("TRIAGE RESULTS:\n#12 agent-ready — clear repro\n"), RunRequest(
            prompt=prompt, cwd=cwd
        )

    monkeypatch.setattr(triage_module, "role_request", fake_role_request)
    monkeypatch.setattr(worktree, "create_detached", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(worktree, "remove", lambda *args, **kwargs: None)
    monkeypatch.setattr(github, "sync_labels", lambda *a, **k: github.LabelSync([], [], [], []))

    results = run_triage(tmp_path, log=lambda _msg: None)

    assert [(r.number, r.verdict) for r in results] == [(12, "agent-ready")]
    assert "#13" not in prompts[0]


def test_run_triage_skips_a_bucket_label_without_triage_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a `needs-human` issue with no `triage:done` stamp -- a
    human's hold, or a legacy bucket applied before the stamp existed --
    must never reach the classification prompt. The model classifying below
    never sees existing labels (only number, title and body reach it), so it
    cannot be trusted to honor the hold; the old `triage:done`-AND-bucket
    predicate let this fall through, get reclassified, and stack a
    contradicting verdict on top of the hold instead of respecting it."""
    prompts: list[str] = []
    monkeypatch.setattr(
        triage_module,
        "run",
        lambda cmd, **kwargs: (
            _FakeProc(json.dumps([_issue(12), {**_issue(13), "labels": [{"name": "needs-human"}]}]))
            if cmd[:3] == ["gh", "issue", "list"]
            else _FakeProc("")
        ),
    )

    def fake_role_request(
        config: ProjectConfig, role_name: str, prompt: str, cwd: Path, **kwargs: object
    ) -> tuple[object, RunRequest]:
        prompts.append(prompt)
        return _FakeRuntime("TRIAGE RESULTS:\n#12 agent-ready — clear repro\n"), RunRequest(
            prompt=prompt, cwd=cwd
        )

    monkeypatch.setattr(triage_module, "role_request", fake_role_request)
    monkeypatch.setattr(worktree, "create_detached", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(worktree, "remove", lambda *args, **kwargs: None)
    monkeypatch.setattr(github, "sync_labels", lambda *a, **k: github.LabelSync([], [], [], []))

    results = run_triage(tmp_path, log=lambda _msg: None)

    assert [(r.number, r.verdict) for r in results] == [(12, "agent-ready")]
    assert "#13" not in prompts[0]


def test_run_triage_reprocesses_a_legacy_triage_done_only_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#257 follow-up: `triage:done` with no bucket -- a legacy issue from
    before Housekeeping applied one alongside the stamp, or one that would
    exist if that ever regressed -- is picked back up and bucketed normally
    rather than left orphaned forever."""
    prompts: list[str] = []
    monkeypatch.setattr(
        triage_module,
        "run",
        lambda cmd, **kwargs: (
            _FakeProc(json.dumps([{**_issue(13), "labels": [{"name": TRIAGE_DONE_LABEL}]}]))
            if cmd[:3] == ["gh", "issue", "list"]
            else _FakeProc("")
        ),
    )

    def fake_role_request(
        config: ProjectConfig, role_name: str, prompt: str, cwd: Path, **kwargs: object
    ) -> tuple[object, RunRequest]:
        prompts.append(prompt)
        return _FakeRuntime(
            "TRIAGE RESULTS:\n#13 backlog — idea, no acceptance criteria\n"
        ), RunRequest(prompt=prompt, cwd=cwd)

    monkeypatch.setattr(triage_module, "role_request", fake_role_request)
    monkeypatch.setattr(worktree, "create_detached", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(worktree, "remove", lambda *args, **kwargs: None)
    monkeypatch.setattr(github, "sync_labels", lambda *a, **k: github.LabelSync([], [], [], []))

    results = run_triage(tmp_path, log=lambda _msg: None)

    assert prompts and "#13" in prompts[0]
    assert [(r.number, r.verdict) for r in results] == [(13, "backlog")]


def test_run_triage_skips_agent_claimed_issues_outright(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#257 follow-up: an issue a local agent is actively implementing must
    never reach a triage pass, or a read-only classification could stamp it
    `needs-human` + `triage:done` underneath the live run."""
    prompts: list[str] = []
    monkeypatch.setattr(
        triage_module,
        "run",
        lambda cmd, **kwargs: (
            _FakeProc(
                json.dumps([_issue(12), {**_issue(14), "labels": [{"name": claims.CLAIM_LABEL}]}])
            )
            if cmd[:3] == ["gh", "issue", "list"]
            else _FakeProc("")
        ),
    )

    def fake_role_request(
        config: ProjectConfig, role_name: str, prompt: str, cwd: Path, **kwargs: object
    ) -> tuple[object, RunRequest]:
        prompts.append(prompt)
        return _FakeRuntime("TRIAGE RESULTS:\n#12 agent-ready — clear repro\n"), RunRequest(
            prompt=prompt, cwd=cwd
        )

    monkeypatch.setattr(triage_module, "role_request", fake_role_request)
    monkeypatch.setattr(worktree, "create_detached", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(worktree, "remove", lambda *args, **kwargs: None)
    monkeypatch.setattr(github, "sync_labels", lambda *a, **k: github.LabelSync([], [], [], []))

    results = run_triage(tmp_path, log=lambda _msg: None)

    assert [(r.number, r.verdict) for r in results] == [(12, "agent-ready")]
    assert "#14" not in prompts[0]


def test_run_triage_stamps_triage_done_alongside_the_bucket_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#257: local both respects and now writes `triage:done`, matching what
    the shared prompt expects both surfaces to do."""
    calls = _stub_triage_run(
        monkeypatch,
        tmp_path,
        "TRIAGE RESULTS:\n#12 agent-ready — clear repro\n",
        [_issue(12)],
    )
    monkeypatch.setattr(github, "sync_labels", lambda *a, **k: github.LabelSync([], [], [], []))

    run_triage(tmp_path, log=lambda _msg: None)

    edit_calls = [c for c in calls if c[:3] == ["gh", "issue", "edit"]]
    assert len(edit_calls) == 1
    edit = edit_calls[0]
    assert edit[3] == "12"
    add_labels = [edit[i + 1] for i, arg in enumerate(edit) if arg == "--add-label"]
    assert set(add_labels) == {"agent-ready", TRIAGE_DONE_LABEL}


def test_bucket_labels_and_triage_done_never_overlap() -> None:
    assert TRIAGE_DONE_LABEL not in BUCKET_LABELS


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
