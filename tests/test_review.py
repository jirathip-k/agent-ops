from pathlib import Path

import pytest

from agent_ops import github
from agent_ops.config import ProjectConfig, load_project_config
from agent_ops.runtimes import RunRequest, RunResult
from agent_ops.utils import CommandError
from agent_ops.workflows import review as review_mod
from agent_ops.workflows.review import budget_diff, run_review


def _file_diff(path: str, body_lines: int = 0) -> str:
    """A minimal single-file diff chunk; total line count is `1 + body_lines`."""
    lines = [f"diff --git a/{path} b/{path}"]
    lines += [f"+line{i}" for i in range(body_lines)]
    return "\n".join(lines) + "\n"


def _rename_diff(old: str, new: str, body_lines: int = 0) -> str:
    lines = [f"diff --git a/{old} b/{new}"]
    lines += [f"+line{i}" for i in range(body_lines)]
    return "\n".join(lines) + "\n"


# --- budget_diff -------------------------------------------------------


def test_budget_diff_under_budget_returned_verbatim() -> None:
    diff = _file_diff("a.py", 1) + _file_diff("b.py", 1)
    kept, omitted = budget_diff(diff, 100)
    assert kept == diff
    assert omitted == []


def test_budget_diff_drops_largest_file_over_budget() -> None:
    small = _file_diff("small.py", 1)  # 2 lines
    big = _file_diff("big.py", 20)  # 21 lines
    diff = small + big  # 23 lines total
    kept, omitted = budget_diff(diff, 10)
    assert omitted == [("big.py", 21)]
    assert kept == small
    assert len(kept.splitlines()) <= 10


def test_budget_diff_single_file_over_budget_leaves_kept_empty() -> None:
    diff = _file_diff("solo.py", 20)  # 21 lines
    kept, omitted = budget_diff(diff, 10)
    assert kept == ""
    assert omitted == [("solo.py", 21)]


def test_budget_diff_empty_diff_passes_through() -> None:
    assert budget_diff("", 100) == ("", [])


def test_budget_diff_reports_b_side_path_for_renames() -> None:
    small = _file_diff("small.py", 1)  # 2 lines
    renamed = _rename_diff("old.py", "new.py", 20)  # 21 lines
    diff = small + renamed
    kept, omitted = budget_diff(diff, 10)
    assert omitted == [("new.py", 21)]
    assert kept == small


def test_budget_diff_ties_break_by_path_ascending() -> None:
    # Equal-size chunks, deliberately out of alphabetical order in the diff,
    # both needing to be dropped — the drop order must still be deterministic.
    zeta = _file_diff("zeta.py", 10)  # 11 lines
    alpha = _file_diff("alpha.py", 10)  # 11 lines
    diff = zeta + alpha  # 22 lines total
    kept, omitted = budget_diff(diff, 5)
    assert omitted == [("alpha.py", 11), ("zeta.py", 11)]
    assert kept == ""


# --- run_review ----------------------------------------------------------


class _FakeRuntime:
    name = "fake"

    def __init__(self, text: str = "VERDICT: APPROVE", ok: bool = True) -> None:
        self.text = text
        self.ok = ok
        self.received_prompt: str | None = None

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest) -> RunResult:
        self.received_prompt = request.prompt
        return RunResult(ok=self.ok, text=self.text)


def _stub_role_request(monkeypatch: pytest.MonkeyPatch, runtime: _FakeRuntime) -> None:
    def fake_role_request(
        config: ProjectConfig, role_name: str, prompt: str, cwd: Path, **kwargs: object
    ) -> tuple[_FakeRuntime, RunRequest]:
        return runtime, RunRequest(prompt=prompt, cwd=cwd)

    monkeypatch.setattr(review_mod, "role_request", fake_role_request)


def _stub_pr(monkeypatch: pytest.MonkeyPatch, diff_text: str, pr_number: int = 42) -> None:
    monkeypatch.setattr(
        github, "pr_view", lambda number, cwd: {"number": pr_number, "title": "t", "body": "b"}
    )
    monkeypatch.setattr(github, "pr_diff", lambda number, cwd: diff_text)


def _write_max_diff_lines(project_root: Path, max_diff_lines: int) -> None:
    agent_dir = project_root / ".agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(f"review:\n  max_diff_lines: {max_diff_lines}\n")


def test_run_review_under_budget_includes_full_diff_no_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_max_diff_lines(tmp_path, 50)
    diff_text = _file_diff("a.py", 5)  # 6 lines, well under budget
    _stub_pr(monkeypatch, diff_text)
    runtime = _FakeRuntime()
    _stub_role_request(monkeypatch, runtime)

    messages: list[str] = []
    result = run_review(tmp_path, 42, log=messages.append)

    assert result == "VERDICT: APPROVE"
    assert runtime.received_prompt is not None
    assert diff_text in runtime.received_prompt
    assert "NOTE:" not in runtime.received_prompt
    assert not any("NOTE:" in m for m in messages)


def test_run_review_over_budget_survivable_adds_note_and_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_max_diff_lines(tmp_path, 10)
    small = _file_diff("small.py", 1)  # 2 lines
    big = _file_diff("big.py", 20)  # 21 lines
    diff_text = small + big  # 23 lines total, over budget
    _stub_pr(monkeypatch, diff_text)
    runtime = _FakeRuntime()
    _stub_role_request(monkeypatch, runtime)

    messages: list[str] = []
    result = run_review(tmp_path, 42, log=messages.append)

    assert result == "VERDICT: APPROVE"
    assert runtime.received_prompt is not None
    assert "NOTE: 1 file(s) omitted" in runtime.received_prompt
    assert "big.py (21 lines)" in runtime.received_prompt
    assert small.strip() in runtime.received_prompt
    # the dropped file's body must not have leaked into the kept prompt
    assert "+line19" not in runtime.received_prompt
    assert any("NOTE: 1 file(s) omitted" in m for m in messages)


def test_run_review_fully_over_budget_raises_before_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_max_diff_lines(tmp_path, 10)
    diff_text = _file_diff("solo.py", 20)  # 21 lines, alone exceeds the budget
    _stub_pr(monkeypatch, diff_text)

    def fail_role_request(*args: object, **kwargs: object) -> tuple[object, object]:
        raise AssertionError("role_request should not be called when nothing fits the budget")

    monkeypatch.setattr(review_mod, "role_request", fail_role_request)

    with pytest.raises(CommandError, match="#42"):
        run_review(tmp_path, 42)


# --- ProjectConfig wiring --------------------------------------------------


def test_review_config_default_is_5000() -> None:
    assert ProjectConfig().review.max_diff_lines == 5000


def test_review_config_project_override(tmp_path: Path) -> None:
    _write_max_diff_lines(tmp_path, 250)
    config = load_project_config(tmp_path)
    assert config.review.max_diff_lines == 250
