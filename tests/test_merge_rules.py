import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_ops import github as github_module
from agent_ops import runs as runs_module
from agent_ops import worktree as worktree_module
from agent_ops.config import ProjectConfig
from agent_ops.runs import Run
from agent_ops.utils import CommandError
from agent_ops.workflows import merge as merge_module
from agent_ops.workflows.merge import (
    _is_test_file,
    closable_issue_refs,
    evaluate_merge,
    run_merge,
    run_merge_check,
)


def _config() -> ProjectConfig:
    return ProjectConfig.model_validate(
        {"base_branch": "staging", "merge": {"stable_branch": "main"}}
    )


def _pr(
    files: list[dict[str, Any]], base: str = "staging", labels: list[str] | None = None
) -> dict[str, Any]:
    pr: dict[str, Any] = {"baseRefName": base, "files": files}
    if labels is not None:
        pr["labels"] = [{"name": name} for name in labels]
    return pr


def test_clean_small_pr_passes() -> None:
    pr = _pr([{"path": "src/app.ts", "additions": 30, "deletions": 5}])
    assert evaluate_merge(pr, _config()) == []


def test_pr_into_stable_branch_is_blocked() -> None:
    pr = _pr([{"path": "src/app.ts", "additions": 1, "deletions": 0}], base="main")
    violations = evaluate_merge(pr, _config())
    assert any("human-only" in v for v in violations)


# ---------- single-branch model (base_branch == merge.stable_branch, issue #182) ----------


def test_single_branch_clean_pr_is_mergeable() -> None:
    config = ProjectConfig.model_validate(
        {"base_branch": "main", "merge": {"stable_branch": "main"}}
    )
    pr = _pr([{"path": "src/app.ts", "additions": 30, "deletions": 5}], base="main")
    assert evaluate_merge(pr, config) == []


def test_single_branch_clean_pr_is_mergeable_with_bare_default() -> None:
    # No `merge` block at all: merge.stable_branch defaults to "main", which
    # is also the bare-default base_branch — the common no-override shape.
    config = ProjectConfig.model_validate({"base_branch": "main"})
    pr = _pr([{"path": "src/app.ts", "additions": 30, "deletions": 5}], base="main")
    assert evaluate_merge(pr, config) == []


def test_single_branch_wrong_base_is_still_blocked_without_contradictory_note() -> None:
    config = ProjectConfig.model_validate(
        {"base_branch": "main", "merge": {"stable_branch": "main"}}
    )
    pr = _pr([{"path": "src/app.ts", "additions": 1, "deletions": 0}], base="feature-x")
    violations = evaluate_merge(pr, config)
    assert any("agents may only merge into 'main'" in v for v in violations)
    assert not any("human-only" in v for v in violations)


def test_single_branch_other_rules_still_apply() -> None:
    config = ProjectConfig.model_validate(
        {"base_branch": "main", "merge": {"stable_branch": "main"}}
    )

    big = _pr([{"path": "src/app.ts", "additions": 500, "deletions": 0}], base="main")
    assert any("changed lines" in v for v in evaluate_merge(big, config))

    blocked_path = _pr([{"path": "package-lock.json", "additions": 1, "deletions": 0}], base="main")
    assert any("blocked path" in v for v in evaluate_merge(blocked_path, config))

    labeled = _pr(
        [{"path": "src/app.ts", "additions": 1, "deletions": 0}],
        base="main",
        labels=["human-merge-only"],
    )
    violations = evaluate_merge(labeled, config)
    assert any("blocked label" in v and "human-merge-only" in v for v in violations)


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


def test_pr_carrying_blocked_label_is_blocked() -> None:
    pr = _pr([{"path": "src/app.ts", "additions": 1, "deletions": 0}], labels=["human-merge-only"])
    violations = evaluate_merge(pr, _config())
    assert any("blocked label" in v and "human-merge-only" in v for v in violations)


def test_pr_without_blocked_label_is_not_blocked() -> None:
    pr = _pr([{"path": "src/app.ts", "additions": 1, "deletions": 0}], labels=["enhancement"])
    assert evaluate_merge(pr, _config()) == []


def test_pr_with_no_labels_key_is_not_blocked() -> None:
    pr = _pr([{"path": "src/app.ts", "additions": 1, "deletions": 0}])
    assert "labels" not in pr
    assert evaluate_merge(pr, _config()) == []


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


def test_check_blocked_label_pr_reports_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--check` is how the CI lane asks, so it must see labels too.

    `_fetch_pr` feeds both `run_merge` and `run_merge_check`; if only the
    former requested `labels`, `human-merge-only` would be enforced locally
    and silently not in CI — which is the lane `agent evolve` opens its draft
    PRs in, so the containment would be exactly backwards.
    """
    pr = _pr([{"path": "src/app.ts", "additions": 1, "deletions": 0}], labels=["human-merge-only"])
    pr["state"] = "OPEN"
    calls = _stub_gh(monkeypatch, pr)

    violations = run_merge_check(tmp_path, 4, log=lambda _msg: None)

    assert any("blocked label" in v and "human-merge-only" in v for v in violations)
    assert any("labels" in c[-1] for c in calls if c[:3] == ["gh", "pr", "view"])


# ---------- run_merge: in-flight-run advisory (issue #258) ----------


def _stub_merge_gh(monkeypatch: pytest.MonkeyPatch, pr: dict[str, Any]) -> list[list[str]]:
    """Fake every `gh`/`git` call `run_merge` makes; records each one."""
    calls: list[list[str]] = []

    def fake_run(
        cmd: list[str], *, cwd: Path | None = None, check: bool = True, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(pr), stderr="")
        if cmd[:3] == ["gh", "pr", "checks"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "push"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(merge_module, "run", fake_run)
    monkeypatch.setattr(merge_module, "load_project_config", lambda root: _config())
    return calls


def _open_pr() -> dict[str, Any]:
    pr = _pr([{"path": "src/app.ts", "additions": 1, "deletions": 0}])
    pr["state"] = "OPEN"
    pr["title"] = "a change"
    pr["headRefName"] = "fix/issue-1"
    return pr


def test_in_flight_runs_block_without_force_or_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_merge_gh(monkeypatch, _open_pr())
    monkeypatch.setattr(
        merge_module, "discover_runs", lambda root, log: ([Run(217, "running", "pid 1")], True)
    )
    messages: list[str] = []

    ok = run_merge(tmp_path, 1, log=messages.append)

    assert ok is False
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)
    assert any("217" in m for m in messages)
    assert any("batch" in m for m in messages)
    assert any("--force" in m for m in messages)


def test_in_flight_runs_with_confirm_accepted_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_merge_gh(monkeypatch, _open_pr())
    monkeypatch.setattr(
        merge_module, "discover_runs", lambda root, log: ([Run(217, "running", "pid 1")], True)
    )

    ok = run_merge(tmp_path, 1, confirm=lambda _msg: True, log=lambda _msg: None)

    assert ok is True
    assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_in_flight_runs_with_confirm_declined_does_not_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_merge_gh(monkeypatch, _open_pr())
    monkeypatch.setattr(
        merge_module, "discover_runs", lambda root, log: ([Run(217, "running", "pid 1")], True)
    )

    ok = run_merge(tmp_path, 1, confirm=lambda _msg: False, log=lambda _msg: None)

    assert ok is False
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_force_merges_without_consulting_confirm_or_run_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_merge_gh(monkeypatch, _open_pr())

    def _boom(root: Path, log: Any) -> Any:
        raise AssertionError("--force must not consult run state at all")

    monkeypatch.setattr(merge_module, "discover_runs", _boom)

    ok = run_merge(
        tmp_path,
        1,
        force=True,
        confirm=lambda _msg: (_ for _ in ()).throw(AssertionError("must not prompt")),
        log=lambda _msg: None,
    )

    assert ok is True
    assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_no_runs_in_flight_merges_with_output_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing running: behaves exactly as before this feature existed."""
    calls = _stub_merge_gh(monkeypatch, _open_pr())
    monkeypatch.setattr(merge_module, "discover_runs", lambda root, log: ([], True))
    messages: list[str] = []

    ok = run_merge(tmp_path, 1, log=messages.append)

    assert ok is True
    assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)
    assert not any("running" in m.lower() for m in messages)


def test_own_run_still_showing_as_running_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`discover_runs` reporting only the merged PR's own issue as `running`

    (its implementer process hasn't fully exited yet) must not be treated as
    a run this merge would stale — it is the run that produced this PR.
    """
    calls = _stub_merge_gh(monkeypatch, _open_pr())  # headRefName: fix/issue-1
    monkeypatch.setattr(
        merge_module, "discover_runs", lambda root, log: ([Run(1, "running", "pid 1")], True)
    )
    messages: list[str] = []

    ok = run_merge(
        tmp_path,
        1,
        confirm=lambda _msg: (_ for _ in ()).throw(AssertionError("must not prompt")),
        log=messages.append,
    )

    assert ok is True
    assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)
    assert not any("running" in m.lower() for m in messages)


def test_own_run_excluded_through_the_real_discover_runs_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every other test in this file stubs `discover_runs` wholesale, which is
    exactly why an earlier version of the own-run exclusion didn't catch a bug
    in what `discover_runs` actually returns for the calling process: it never
    ran. This one leaves `discover_runs` itself alone and only fakes the
    signals it reads (worktree list, `ps`, open PRs) — a `fix/issue-1`
    worktree that still looks live must not block PR #1's own merge.
    """
    calls = _stub_merge_gh(monkeypatch, _open_pr())  # headRefName: fix/issue-1
    wt_path = tmp_path / ".worktrees" / "issue-1"
    wt_path.mkdir(parents=True)
    monkeypatch.setattr(
        worktree_module,
        "list_worktrees",
        lambda root: [worktree_module.Worktree(wt_path, "fix/issue-1")],
    )
    monkeypatch.setattr(runs_module, "_ps_output", lambda log: "1 00:06:00 agent implement 1\n")
    monkeypatch.setattr(github_module, "open_prs", lambda cwd: [])

    ok = run_merge(
        tmp_path,
        1,
        confirm=lambda _msg: (_ for _ in ()).throw(AssertionError("must not prompt")),
        log=lambda _msg: None,
    )

    assert ok is True
    assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)


def test_unreadable_run_registry_warns_and_proceeds_never_claims_nothing_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _stub_merge_gh(monkeypatch, _open_pr())

    def _raises(root: Path, log: Any) -> Any:
        log("warning: could not list worktrees (boom); runs may be misreported as stopped")
        raise CommandError("boom")

    monkeypatch.setattr(merge_module, "discover_runs", _raises)
    messages: list[str] = []

    ok = run_merge(tmp_path, 1, log=messages.append)

    assert ok is True
    assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)
    assert any("warning" in m.lower() for m in messages)
    assert not any("nothing running" in m.lower() for m in messages)


def test_untrustworthy_run_state_warns_and_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`discover_runs` degrading to untrustworthy (its own gh/git outage path)

    must fail open the same as an outright exception: unknown, not "nothing
    running".
    """
    calls = _stub_merge_gh(monkeypatch, _open_pr())

    def _degraded(root: Path, log: Any) -> Any:
        log("warning: could not list open PRs (boom); runs may be misreported as stopped")
        return [], False

    monkeypatch.setattr(merge_module, "discover_runs", _degraded)
    messages: list[str] = []

    ok = run_merge(tmp_path, 1, log=messages.append)

    assert ok is True
    assert any(c[:3] == ["gh", "pr", "merge"] for c in calls)
    assert not any("nothing running" in m.lower() for m in messages)


def test_degraded_poll_with_running_row_still_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded poll (`trustworthy=False`) only ever under-reports runs as

    stopped, never invents one — so a `running` row it does surface is real
    positive evidence and must still block/prompt, not be discarded because
    other signals this round were degraded (issue #116's Orca-unreachable
    scenario).
    """
    calls = _stub_merge_gh(monkeypatch, _open_pr())

    def _degraded(root: Path, log: Any) -> Any:
        log("warning: could not list open PRs (boom); runs may be misreported as stopped")
        return [Run(217, "running", "pid 1")], False

    monkeypatch.setattr(merge_module, "discover_runs", _degraded)
    messages: list[str] = []

    ok = run_merge(tmp_path, 1, log=messages.append)

    assert ok is False
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)
    assert any("217" in m for m in messages)


def test_violations_checked_before_run_state_is_consulted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked PR is refused on rule violations alone — run state is never asked."""
    pr = _pr([{"path": "package-lock.json", "additions": 1, "deletions": 0}])
    pr["state"] = "OPEN"
    pr["title"] = "a change"
    calls = _stub_merge_gh(monkeypatch, pr)

    def _boom(root: Path, log: Any) -> Any:
        raise AssertionError("run state must not be consulted when violations already block")

    monkeypatch.setattr(merge_module, "discover_runs", _boom)

    ok = run_merge(tmp_path, 1, log=lambda _msg: None)

    assert ok is False
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in calls)
    assert any("labels" in c[-1] for c in calls if c[:3] == ["gh", "pr", "view"])
