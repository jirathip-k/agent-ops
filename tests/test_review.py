import re
from pathlib import Path

import pytest

from agent_ops import github
from agent_ops.config import ProjectConfig, load_project_config
from agent_ops.fallback import ModelUnavailableError
from agent_ops.runtimes import FailureKind, RunRequest, RunResult
from agent_ops.utils import CommandError
from agent_ops.workflows import review as review_mod
from agent_ops.workflows.review import (
    ReviewOutcome,
    budget_diff,
    format_summary,
    run_review,
    run_reviews,
    verdict_of,
)


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

    def classify_failure(self, result: RunResult) -> FailureKind:
        return FailureKind.AGENT_FAILURE


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
    assert "verification context for this read-only lane" in runtime.received_prompt
    assert "Do not run the configured `setup` command here" in runtime.received_prompt
    assert (
        "parent `run_gates` execution remains the final pass/fail authority"
        not in runtime.received_prompt
    )
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


# --- verdict_of ------------------------------------------------------------


def test_verdict_of_approve() -> None:
    assert verdict_of("VERDICT: APPROVE\n\nlooks fine") == "approve"


def test_verdict_of_request_changes() -> None:
    assert verdict_of("some preamble\nVERDICT: REQUEST CHANGES\n\nfix this") == "request_changes"


def test_verdict_of_garbage_is_unknown() -> None:
    assert verdict_of("this review has no verdict line at all") == "unknown"


def test_verdict_of_tolerates_markdown_decoration() -> None:
    assert verdict_of("**VERDICT: APPROVE**") == "approve"


# --- run_reviews (multi-PR fan-out) ----------------------------------------


def _pr_from_prompt(prompt: str) -> int:
    match = re.search(r"PR #(\d+)", prompt)
    assert match is not None
    return int(match.group(1))


class _MultiRuntime:
    """Fake runtime whose result is keyed by the PR number embedded in the
    rendered prompt (`run_review`'s `context=f"PR #{pr['number']}: ..."`).
    """

    name = "fake"

    def __init__(
        self,
        results: dict[int, tuple[bool, str]] | None = None,
        classify: FailureKind = FailureKind.AGENT_FAILURE,
    ) -> None:
        self.results = results or {}
        self.classify = classify
        self.calls: list[int] = []
        self.requests: list[RunRequest] = []

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest) -> RunResult:
        pr = _pr_from_prompt(request.prompt)
        self.calls.append(pr)
        self.requests.append(request)
        ok, text = self.results.get(pr, (True, "VERDICT: APPROVE"))
        return RunResult(ok=ok, text=text)

    def classify_failure(self, result: RunResult) -> FailureKind:
        return self.classify


def _stub_multi(monkeypatch: pytest.MonkeyPatch, runtime: _MultiRuntime) -> None:
    def fake_role_request(
        config: ProjectConfig, role_name: str, prompt: str, cwd: Path, **kwargs: object
    ) -> tuple[_MultiRuntime, RunRequest]:
        # stream=True mirrors the config default (RuntimeConfig.stream); the
        # fan-out is what's responsible for forcing it back to False.
        return runtime, RunRequest(prompt=prompt, cwd=cwd, stream=True)

    def fake_pr_view(number: int, cwd: Path) -> dict[str, object]:
        return {"number": number, "title": f"t{number}", "body": ""}

    monkeypatch.setattr(review_mod, "role_request", fake_role_request)
    monkeypatch.setattr(github, "pr_view", fake_pr_view)
    monkeypatch.setattr(github, "pr_diff", lambda number, cwd: _file_diff(f"f{number}.py", 1))


def test_run_reviews_happy_path_returns_outcomes_in_input_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _MultiRuntime(
        {
            1: (True, "VERDICT: APPROVE"),
            2: (True, "VERDICT: REQUEST CHANGES"),
            3: (True, "no verdict line here"),
        }
    )
    _stub_multi(monkeypatch, runtime)

    outcomes = run_reviews(tmp_path, [1, 2, 3], jobs=3)

    assert [o.pr for o in outcomes] == [1, 2, 3]
    assert [o.status for o in outcomes] == ["approve", "request_changes", "unknown"]
    assert sorted(runtime.calls) == [1, 2, 3]


def test_run_reviews_aborts_queue_on_model_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _MultiRuntime({2: (False, "no model left")}, classify=FailureKind.MODEL_UNAVAILABLE)
    _stub_multi(monkeypatch, runtime)

    outcomes = run_reviews(tmp_path, [1, 2, 3], jobs=1)

    assert [o.pr for o in outcomes] == [1, 2, 3]
    assert [o.status for o in outcomes] == ["approve", "failed", "skipped"]
    # PR 3 must never have reached the runtime — the abort is a hard stop,
    # not a "mark it failed after trying" gesture.
    assert 3 not in runtime.calls


def test_run_reviews_ordinary_failure_does_not_abort_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _MultiRuntime({2: (False, "agent gave up")}, classify=FailureKind.AGENT_FAILURE)
    _stub_multi(monkeypatch, runtime)

    outcomes = run_reviews(tmp_path, [1, 2, 3], jobs=1)

    assert [o.status for o in outcomes] == ["approve", "failed", "approve"]
    assert sorted(runtime.calls) == [1, 2, 3]


def test_run_reviews_post_comment_posts_once_per_pr_with_footer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _MultiRuntime({1: (True, "VERDICT: APPROVE"), 2: (True, "VERDICT: APPROVE")})
    _stub_multi(monkeypatch, runtime)
    posted: list[tuple[int, str]] = []
    monkeypatch.setattr(
        github, "comment_on_pr", lambda number, body, cwd: posted.append((number, body))
    )

    run_reviews(tmp_path, [1, 2], jobs=2, post_comment=True)

    assert sorted(number for number, _ in posted) == [1, 2]
    assert all("_agent-ops · model:" in body for _, body in posted)


def test_run_reviews_forces_stream_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _MultiRuntime()
    _stub_multi(monkeypatch, runtime)

    run_reviews(tmp_path, [1, 2], jobs=2)

    assert all(request.stream is False for request in runtime.requests)


def test_format_summary_distinguishes_statuses() -> None:
    outcomes = [
        ReviewOutcome(1, "approve", text="ok"),
        ReviewOutcome(2, "request_changes", text="fix it"),
        ReviewOutcome(3, "failed", error="boom"),
        ReviewOutcome(4, "skipped"),
    ]
    summary = format_summary(outcomes)
    assert "PR #1: APPROVE" in summary
    assert "PR #2: REQUEST CHANGES" in summary
    assert "PR #3: run failed" in summary
    assert "PR #4: skipped" in summary


def test_run_review_raises_model_unavailable_error_on_exhausted_ladder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _MultiRuntime({42: (False, "no model left")}, classify=FailureKind.MODEL_UNAVAILABLE)
    _stub_multi(monkeypatch, runtime)

    with pytest.raises(ModelUnavailableError):
        run_review(tmp_path, 42)


def test_run_review_post_comment_true_comments_on_the_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pr(monkeypatch, _file_diff("a.py", 1), pr_number=42)
    runtime = _FakeRuntime(text="VERDICT: APPROVE\n\nlooks fine")
    _stub_role_request(monkeypatch, runtime)
    posted: list[tuple[int, str]] = []
    monkeypatch.setattr(
        github, "comment_on_pr", lambda number, body, cwd: posted.append((number, body))
    )

    result = run_review(tmp_path, 42, post_comment=True)

    assert result == "VERDICT: APPROVE\n\nlooks fine"
    assert len(posted) == 1
    number, body = posted[0]
    assert number == 42
    assert body.startswith("## Agent review")
    assert "_agent-ops · model:" in body


def test_run_review_default_does_not_comment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pr(monkeypatch, _file_diff("a.py", 1), pr_number=42)
    runtime = _FakeRuntime(text="VERDICT: APPROVE")
    _stub_role_request(monkeypatch, runtime)

    def fail_comment(*args: object, **kwargs: object) -> None:
        raise AssertionError("comment_on_pr should not be called without post_comment=True")

    monkeypatch.setattr(github, "comment_on_pr", fail_comment)

    result = run_review(tmp_path, 42)

    assert result == "VERDICT: APPROVE"


def test_run_review_failed_run_raises_and_never_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_pr(monkeypatch, _file_diff("a.py", 1), pr_number=42)
    runtime = _FakeRuntime(text="agent gave up", ok=False)
    _stub_role_request(monkeypatch, runtime)

    def fail_comment(*args: object, **kwargs: object) -> None:
        raise AssertionError("comment_on_pr should not be called when the run failed")

    monkeypatch.setattr(github, "comment_on_pr", fail_comment)

    with pytest.raises(RuntimeError, match="Review run failed"):
        run_review(tmp_path, 42, post_comment=True)
