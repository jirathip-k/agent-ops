from typing import Any

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
