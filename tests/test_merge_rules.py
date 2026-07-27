import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_ops.config import ProjectConfig
from agent_ops.workflows import merge as merge_module
from agent_ops.workflows.merge import (
    _is_test_file,
    closable_issue_refs,
    evaluate_merge,
    run_merge_check,
)


def _config() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {"base_branch": "staging", "merge": {"stable_branch": "main"}}
    )


def _pr(files: list[dict[str, Any]], base: str = "staging") -> dict[str, Any]:
    return {"baseRefName": base, "files": files}


def test_clean_small_pr_passes() -> None:
    pr = _pr([{"path": "src/app.ts", "additions": 30, "deletions": 5}])
    assert evaluate_merge(pr, _config()) == []


def test_pr_into_stable_branch_is_blocked() -> None:
    pr = _pr([{"path": "src/app.ts", "additions": 1, "deletions": 0}], base="main")
    violations = evaluate_merge(pr, _config())
    assert any("human-only" in v for v in violations)


def test_size_caps() -> None:
    big = _pr([{"path": "src/app.ts", "additions": 500, "deletions": 0}])
    assert any("changed lines" in v for v in evaluate_merge(big, _config()))

    many = _pr([{"path": f"src/f{i}.ts", "additions": 1, "deletions": 0} for i in range(13)])
    assert any("changed files" in v for v in evaluate_merge(many, _config()))


def test_blocked_paths() -> None:
    for path in (
        ".github/workflows/deploy.yml",
        "src/hooks/useAuth.ts",
        "package-lock.json",
        "db/migrations/001.sql",
    ):
        pr = _pr([{"path": path, "additions": 1, "deletions": 0}])
        violations = evaluate_merge(pr, _config())
        assert any("blocked path" in v for v in violations), path


def test_mixed_pr_excludes_test_lines_from_cap() -> None:
    pr = _pr(
        [
            {"path": "src/app.ts", "additions": 150, "deletions": 50},
            {"path": "tests/test_app.py", "additions": 400, "deletions": 0},
        ]
    )
    assert evaluate_merge(pr, _config()) == []


def test_production_lines_alone_still_trip_cap_message_unchanged() -> None:
    pr = _pr([{"path": "src/app.ts", "additions": 500, "deletions": 0}])
    violations = evaluate_merge(pr, _config())
    # No "(N effective, tests excluded)" clause: nothing was excluded.
    assert "500 changed lines > cap 400" in violations
    assert not any("effective" in v for v in violations if "changed lines" in v)


def test_files_cap_uses_effective_count_with_wording() -> None:
    files = [{"path": f"src/f{i}.ts", "additions": 1, "deletions": 0} for i in range(13)]
    files += [{"path": f"tests/test_f{i}.py", "additions": 1, "deletions": 0} for i in range(5)]
    violations = evaluate_merge(_pr(files), _config())
    assert "18 changed files (13 effective, tests excluded) > cap 12" in violations


def test_test_only_pr_passes_production_cap_and_backstop() -> None:
    pr = _pr([{"path": "tests/test_big.py", "additions": 600, "deletions": 0}])
    assert evaluate_merge(pr, _config()) == []


def test_test_only_pr_over_backstop_fails_via_backstop_message() -> None:
    pr = _pr([{"path": "tests/test_big.py", "additions": 2000, "deletions": 0}])
    violations = evaluate_merge(pr, _config())
    assert "2000 total changed lines (including tests) > backstop cap 1600" in violations
    assert not any(v.startswith("2000 changed lines") for v in violations)


def test_rename_with_zero_lines_plus_tests_has_no_violations() -> None:
    pr = _pr(
        [
            {"path": "src/moved.ts", "additions": 0, "deletions": 0},
            {"path": "tests/test_app.py", "additions": 600, "deletions": 0},
        ]
    )
    assert evaluate_merge(pr, _config()) == []


def test_contests_and_protests_are_not_misclassified_as_tests() -> None:
    pr = _pr([{"path": "src/contests/model.py", "additions": 700, "deletions": 700}])
    assert "1400 changed lines > cap 400" in evaluate_merge(pr, _config())

    pr2 = _pr([{"path": "app/protests/view.py", "additions": 700, "deletions": 700}])
    assert "1400 changed lines > cap 400" in evaluate_merge(pr2, _config())


def test_file_count_backstop_trips_even_when_effective_and_line_backstop_pass() -> None:
    files = [{"path": "src/app.ts", "additions": 3, "deletions": 2}]
    files += [{"path": f"tests/test_f{i}.py", "additions": 3, "deletions": 2} for i in range(300)]
    violations = evaluate_merge(_pr(files), _config())
    assert "301 total changed files (including tests) > backstop cap 48" in violations
    # effective_files (1) passes the base cap, and the line total (1505) passes
    # the line backstop — only the file backstop should fire.
    assert not any("cap 12" in v for v in violations)
    assert not any("backstop cap 1600" in v for v in violations)


def test_blocked_paths_still_block_test_classified_paths() -> None:
    for path in (
        "infra/tests/main.tf",
        "db/migrations/tests/001.sql",
        "src/auth/tests/login.ts",
    ):
        pr = _pr([{"path": path, "additions": 1, "deletions": 0}])
        violations = evaluate_merge(pr, _config())
        assert any("blocked path" in v for v in violations), path


def test_total_cap_ratio_zero_is_rejected_at_load() -> None:
    with pytest.raises(ValidationError):
        ProjectConfig.model_validate({"merge": {"total_cap_ratio": 0}})


def test_total_cap_ratio_one_pins_backstop_to_base_caps() -> None:
    config = ProjectConfig.model_validate(
        {"base_branch": "staging", "merge": {"stable_branch": "main", "total_cap_ratio": 1}}
    )
    pr = _pr([{"path": "src/app.ts", "additions": 401, "deletions": 0}])
    violations = evaluate_merge(pr, config)
    assert any("backstop cap 400" in v for v in violations)


def test_swift_and_xcode_test_patterns_are_recognized() -> None:
    for path in (
        "AppTests.swift",
        "AppTests/FooTests.swift",
        "Sources/AppUITests/BarTests.swift",
        "Tests/AppTests/Foo.swift",
    ):
        pr = _pr([{"path": path, "additions": 1000, "deletions": 0}])
        assert evaluate_merge(pr, _config()) == [], path

    for path in ("src/Contests.swift", "packages/contests/index.ts"):
        pr = _pr([{"path": path, "additions": 1000, "deletions": 0}])
        violations = evaluate_merge(pr, _config())
        assert any("changed lines" in v for v in violations), path


def test_go_test_file_pattern_is_recognized() -> None:
    pr = _pr([{"path": "pkg/foo_test.go", "additions": 1000, "deletions": 0}])
    assert evaluate_merge(pr, _config()) == []


def test_lookalike_production_paths_are_not_exempted_as_tests() -> None:
    for path in ("src/test_data/schema.py", "test_data/schema.py"):
        pr = _pr([{"path": path, "additions": 1000, "deletions": 0}])
        violations = evaluate_merge(pr, _config())
        assert any("changed lines" in v for v in violations), path


def test_is_test_file_stays_case_sensitive_under_windows_style_normcase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # fnmatch resolves os.path.normcase at call time, so patching it here
    # simulates the case-folding fnmatch.fnmatch would apply on Windows.
    monkeypatch.setattr(os.path, "normcase", str.lower)
    assert _is_test_file("src/contests/model.py", ["*Tests/*"]) is False
    assert _is_test_file("src/Contests.swift", ["*Tests.swift"]) is False
    assert _is_test_file("AppTests/FooTests.swift", ["*Tests/*"]) is True


def test_blocked_paths_case_insensitivity_unaffected_by_test_file_fix() -> None:
    pr = _pr([{"path": "src/useAuth.ts", "additions": 1, "deletions": 0}])
    violations = evaluate_merge(pr, _config())
    assert any("blocked path" in v for v in violations)


def test_closable_refs_trailing_issue_numbers_only() -> None:
    subjects = [
        "force: queue failed recording saves + retry after re-auth (#106)",
        "deploy: enable Vercel previews for staging with dev backend (#121)",
        "Merge PR #103: rank send conditions by same-hour daily history",
        "SL-99 follow-ups: day-ago labels account for archive lag",
        "health-core: harvest baselines past data gaps so readiness recovers (#111) (#116)",
    ]
    open_issues = {106, 111, 121}
    # #103 is mid-subject (a PR reference) so it never closes; #111 still
    # closes despite the squash-appended "(#116)" PR wrapper after it.
    assert closable_issue_refs(subjects, open_issues) == [106, 111, 121]


def test_closable_refs_drops_closed_issues_and_pr_numbers() -> None:
    subjects = ["fix: thing (#5)", "fix: other (#6)", "fix: third (#7)"]
    assert closable_issue_refs(subjects, {6}) == [6]


# ---------- run_merge_check: CI lane and `agent merge` share one verdict (#150) ----------


def _stub_gh(monkeypatch: pytest.MonkeyPatch, pr: dict[str, Any]) -> list[list[str]]:
    """Fake `gh pr view` returning `pr`; records every command and refuses to merge."""
    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str], *, cwd: Path | None = None, check: bool = True, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(pr), stderr="")
        raise AssertionError(f"run_merge_check must not invoke: {cmd}")

    monkeypatch.setattr(merge_module, "run", fake_run)
    monkeypatch.setattr(merge_module, "load_project_config", lambda root: _config())
    return calls


def test_check_clean_pr_reports_no_violations_and_never_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pr = _pr([{"path": "src/app.ts", "additions": 30, "deletions": 5}])
    pr["state"] = "OPEN"
    calls = _stub_gh(monkeypatch, pr)

    violations = run_merge_check(tmp_path, 1, log=lambda _msg: None)

    assert violations == []
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_check_over_cap_pr_reports_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pr = _pr([{"path": "src/app.ts", "additions": 500, "deletions": 0}])
    pr["state"] = "OPEN"
    calls = _stub_gh(monkeypatch, pr)

    violations = run_merge_check(tmp_path, 2, log=lambda _msg: None)

    assert any("changed lines" in v for v in violations)
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_check_blocked_path_pr_reports_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pr = _pr([{"path": "package-lock.json", "additions": 1, "deletions": 0}])
    pr["state"] = "OPEN"
    _stub_gh(monkeypatch, pr)

    violations = run_merge_check(tmp_path, 3, log=lambda _msg: None)

    assert any("blocked path" in v for v in violations)


def test_check_non_open_pr_is_blocking(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pr = _pr([{"path": "src/app.ts", "additions": 1, "deletions": 0}])
    pr["state"] = "MERGED"
    calls = _stub_gh(monkeypatch, pr)

    violations = run_merge_check(tmp_path, 4, log=lambda _msg: None)

    assert violations != []
    assert any("MERGED" in v for v in violations)
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_check_verdict_matches_evaluate_merge_for_same_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pr = _pr(
        [
            {"path": "src/app.ts", "additions": 300, "deletions": 0},
            {"path": "tests/test_app.py", "additions": 400, "deletions": 0},
        ]
    )
    pr["state"] = "OPEN"
    _stub_gh(monkeypatch, pr)

    assert run_merge_check(tmp_path, 5, log=lambda _msg: None) == evaluate_merge(pr, _config())


def test_check_passes_150_production_plus_300_test_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PR from issue #150's worked example: refused by the old CI prose

    cap (200 total changed lines), mergeable under production-only caps with
    a raw backstop. `--check` must land on the same side as `evaluate_merge`.
    """
    pr = _pr(
        [
            {"path": "src/app.ts", "additions": 150, "deletions": 0},
            {"path": "tests/test_app.py", "additions": 300, "deletions": 0},
        ]
    )
    pr["state"] = "OPEN"
    _stub_gh(monkeypatch, pr)

    assert run_merge_check(tmp_path, 6, log=lambda _msg: None) == []


def test_closable_refs_empty_when_no_refs() -> None:
    assert closable_issue_refs(["docs: update guide"], {1, 2}) == []
