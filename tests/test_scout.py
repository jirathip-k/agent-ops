from pathlib import Path

import pytest

from agent_ops import github, worktree
from agent_ops.config import ProjectConfig
from agent_ops.prompts import TASKS_DIR, UNTRUSTED_DATA_GUARD, render_task
from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult
from agent_ops.utils import CommandError
from agent_ops.workflows import scout as scout_module
from agent_ops.workflows.scout import FOCUS_MAX_CHARS, focus_block, parse_scout, run_scout


def _render(focus: str) -> str:
    return render_task("scout", max_issues="3", repo_focus=focus_block(focus))


def test_parses_filed_issues() -> None:
    text = """Mined TODOs and merged-PR threads.

SCOUT RESULTS:
#41 — TODO at src/lib/export.ts:88, silent failure when the sheet is empty
#42 — PR #120 review deferred pagination to a follow-up that was never filed
"""
    results = parse_scout(text)
    assert results is not None
    assert [(r.number, r.reason.split(",")[0]) for r in results] == [
        (41, "TODO at src/lib/export.ts:88"),
        (42, "PR #120 review deferred pagination to a follow-up that was never filed"),
    ]


def test_explicit_none_is_empty_list() -> None:
    assert parse_scout("nothing cleared the bar\n\nSCOUT RESULTS:\nnone\n") == []


def test_no_marker_is_none() -> None:
    assert parse_scout("no block here") is None


def test_uses_last_marker_and_ignores_junk() -> None:
    text = "SCOUT RESULTS:\n#1 — draft\nSCOUT RESULTS:\n#2 — final\nnoise\n"
    results = parse_scout(text)
    assert results is not None
    assert [(r.number, r.reason) for r in results] == [(2, "final")]


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


def _stub_scout_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, verdict_block: str
) -> list[list[str]]:
    """Drive run_scout with a fake runtime; return every `gh`/`git` argv it ran."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
        calls.append(cmd)
        return _FakeProc("")

    def fake_role_request(
        config: ProjectConfig, role_name: str, prompt: str, cwd: Path, **kwargs: object
    ) -> tuple[object, RunRequest]:
        return _FakeRuntime(verdict_block), RunRequest(prompt=prompt, cwd=cwd)

    monkeypatch.setattr(scout_module, "run", fake_run)
    monkeypatch.setattr(scout_module, "role_request", fake_role_request)
    monkeypatch.setattr(worktree, "create_detached", lambda *args, **kwargs: tmp_path / "wt")
    monkeypatch.setattr(worktree, "remove", lambda *args, **kwargs: None)
    return calls


def test_run_scout_syncs_the_labels_it_needs_to_file_issues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_scout_run(monkeypatch, tmp_path, "SCOUT RESULTS:\n#41 — a TODO\n")
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

    results = run_scout(tmp_path, log=lambda _msg: None)

    assert [r.number for r in results] == [41]
    assert synced and set(synced[0]) == {"backlog", "proposed-by-agent"}
    # scout pins the repo it resolved, so a fork's `gh` base-repo resolution
    # can't redirect the sync to the wrong repository.
    assert seen_repo == ["acme/widgets"]


def test_run_scout_survives_a_label_sync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_scout_run(monkeypatch, tmp_path, "SCOUT RESULTS:\n#41 — a TODO\n")

    def fake_sync_labels(
        project_root: Path, labels: dict[str, github.Label], *, repo: str | None = None
    ) -> github.LabelSync:
        raise CommandError("no write scope")

    monkeypatch.setattr(github, "sync_labels", fake_sync_labels)
    logged: list[str] = []

    results = run_scout(tmp_path, log=logged.append)

    assert [r.number for r in results] == [41]
    assert any("could not sync labels" in line for line in logged)


def test_run_scout_survives_gh_not_being_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing `gh` binary surfaces from `sync_labels` as `CommandError` —
    `utils.run` converts the underlying `FileNotFoundError` under `check=True`
    (agent-ops#154) — a missing `gh` here must not abort a scout run after
    issues have already been parsed."""
    _stub_scout_run(monkeypatch, tmp_path, "SCOUT RESULTS:\n#41 — a TODO\n")

    def missing_gh(
        project_root: Path, labels: dict[str, github.Label], *, repo: str | None = None
    ) -> github.LabelSync:
        raise CommandError("could not list labels: `gh` could not be started")

    monkeypatch.setattr(github, "sync_labels", missing_gh)
    logged: list[str] = []

    results = run_scout(tmp_path, log=logged.append)

    assert [r.number for r in results] == [41]
    assert any("could not sync labels" in line for line in logged)


def test_run_scout_logs_a_single_label_failure_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_scout_run(monkeypatch, tmp_path, "SCOUT RESULTS:\n#41 — a TODO\n")

    def fake_sync_labels(
        project_root: Path, labels: dict[str, github.Label], *, repo: str | None = None
    ) -> github.LabelSync:
        return github.LabelSync(
            created=[], updated=[], unchanged=[], failed=[("backlog", "HTTP 403: no scope")]
        )

    monkeypatch.setattr(github, "sync_labels", fake_sync_labels)
    logged: list[str] = []

    results = run_scout(tmp_path, log=logged.append)

    assert [r.number for r in results] == [41]
    assert any("backlog" in line and "HTTP 403: no scope" in line for line in logged)


def test_focus_reaches_the_prompt_with_its_grounding_reminder() -> None:
    prompt = _render("Pages with no meta description; posts not updated in 12 months.")
    assert "## Repo focus" in prompt
    assert "posts not updated in 12 months" in prompt
    assert "still need a concrete signal" in prompt


def test_focus_cannot_outrank_the_rules_around_it() -> None:
    """A repo says what to look for; the caps and the danger-zone ban still bind."""
    prompt = _render("Pages with no meta description.")
    assert prompt.index("Ground every candidate in a signal") < prompt.index("## Repo focus")
    assert prompt.index("## Repo focus") < prompt.index("## Filing")


def test_no_focus_renders_todays_prompt_byte_for_byte() -> None:
    """Repos that never configure a focus must see no trace of the feature —
    not even the blank line a naive placeholder would leave behind."""
    today = (TASKS_DIR / "scout.md").read_text().replace("{repo_focus}", "")
    assert _render("") == f"{UNTRUSTED_DATA_GUARD}\n\n{today.format(max_issues='3')}"
    assert "## Repo focus" not in _render("")
    # the join today's prompt has, spelled out so the template can't drift into
    # an extra blank line and still pass
    assert "5. Docs or comments that contradict the current code\n\n## Filing" in _render("")


def test_whitespace_only_focus_is_no_focus() -> None:
    assert _render("   \n\t  \n") == _render("")


def test_braces_in_focus_are_data_not_a_placeholder() -> None:
    # focus is a str.format *value*, never part of the template
    prompt = _render("Routes like {slug} rendering a 404")
    assert "Routes like {slug} rendering a 404" in prompt


def test_overlong_focus_is_truncated_and_the_prompt_stays_bounded() -> None:
    baseline = len(_render(""))
    prompt = _render("meta descriptions " * 500)
    assert "…truncated]" in prompt
    assert len(prompt) - baseline < FOCUS_MAX_CHARS + 300
