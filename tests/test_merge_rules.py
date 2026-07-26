from typing import Any

import pytest
from pydantic import ValidationError

from agent_ops.config import ProjectConfig
from agent_ops.workflows.merge import closable_issue_refs, evaluate_merge


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


def test_closable_refs_empty_when_no_refs() -> None:
    assert closable_issue_refs(["docs: update guide"], {1, 2}) == []
