from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_ops import orca, orca_sync, registry
from agent_ops.cli import app
from agent_ops.registry import RegistryConfig
from agent_ops.utils import CommandError

runner = CliRunner()


def _pr(
    number: int = 42,
    head: str = "fix/issue-7",
    base: str = "staging",
    rollup: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"fix thing {number}",
        "url": f"https://github.com/o/r/pull/{number}",
        "headRefName": head,
        "baseRefName": base,
        "isDraft": False,
        "reviewDecision": "",
        "statusCheckRollup": [] if rollup is None else rollup,
        **extra,
    }


def _check(conclusion: str = "SUCCESS", status: str = "COMPLETED") -> dict[str, Any]:
    return {"__typename": "CheckRun", "name": "test", "status": status, "conclusion": conclusion}


# --- check rollup collapsing -------------------------------------------------


def test_check_state_no_checks() -> None:
    assert orca_sync.check_state([]) == "none"
    assert orca_sync.check_state(None) == "none"


def test_check_state_all_green() -> None:
    assert orca_sync.check_state([_check(), _check()]) == "passing"


def test_check_state_failure_wins_over_pending() -> None:
    rollup = [_check(), _check("", "IN_PROGRESS"), _check("FAILURE")]
    assert orca_sync.check_state(rollup) == "failing"


def test_check_state_running_check_is_pending() -> None:
    assert orca_sync.check_state([_check(), _check("", "IN_PROGRESS")]) == "pending"


def test_check_state_commit_status_contexts() -> None:
    contexts = [{"__typename": "StatusContext", "context": "ci", "state": "PENDING"}]
    assert orca_sync.check_state(contexts) == "pending"
    assert orca_sync.check_state([{"__typename": "StatusContext", "state": "ERROR"}]) == "failing"
    assert orca_sync.check_state([{"__typename": "StatusContext", "state": "SUCCESS"}]) == "passing"


def test_check_state_skipped_and_neutral_are_not_failures() -> None:
    assert orca_sync.check_state([_check("SKIPPED"), _check("NEUTRAL")]) == "passing"


# --- lane discovery ----------------------------------------------------------


def test_discover_lanes_maps_agent_pr_to_implement_lane() -> None:
    (lane,) = orca_sync.discover_lanes("o/r", [_pr(rollup=[_check()])], [])
    assert lane.kind == "implement"
    assert lane.branch == "fix/issue-7"
    assert lane.card_name == "issue-7"  # same worktree name `agent dispatch` uses
    assert lane.status == orca.STATUS_IN_REVIEW
    assert "PR #42" in lane.comment
    assert "issue #7" in lane.comment
    assert "checks passing" in lane.comment


def test_discover_lanes_failing_checks_stay_in_progress() -> None:
    (lane,) = orca_sync.discover_lanes("o/r", [_pr(rollup=[_check("FAILURE")])], [])
    assert lane.status == orca.STATUS_IN_PROGRESS
    assert "checks failing" in lane.comment


def test_discover_lanes_draft_is_never_in_review() -> None:
    (lane,) = orca_sync.discover_lanes("o/r", [_pr(rollup=[_check()], isDraft=True)], [])
    assert lane.status == orca.STATUS_IN_PROGRESS
    assert "draft" in lane.comment


def test_discover_lanes_review_decision_reaches_the_comment() -> None:
    pr = _pr(rollup=[_check()], reviewDecision="CHANGES_REQUESTED")
    (lane,) = orca_sync.discover_lanes("o/r", [pr], [])
    assert "review changes requested" in lane.comment


def test_discover_lanes_hotfix_branch_is_its_own_kind() -> None:
    (lane,) = orca_sync.discover_lanes("o/r", [_pr(head="hotfix/issue-9", base="main")], [])
    assert lane.kind == "hotfix"
    assert lane.card_name == "hotfix-issue-9"  # never collides with a fix/issue-9 worktree


def test_discover_lanes_promotion_pr_is_reported_but_gets_no_new_card() -> None:
    prs = [_pr(number=50, head="staging", base="main", rollup=[_check()])]
    (lane,) = orca_sync.discover_lanes(
        "o/r", prs, [], working_branch="staging", stable_branch="main"
    )
    assert lane.kind == "promote"
    assert lane.branch == "staging"
    assert lane.ensure_card is False


def test_discover_lanes_ignores_non_agent_prs() -> None:
    prs = [_pr(head="jirathip-k/hand-written"), _pr(head="dependabot/npm/lodash")]
    assert orca_sync.discover_lanes("o/r", prs, []) == []


def test_discover_lanes_defaults_detect_no_promotion_lane() -> None:
    # A repo with no .agent/config.yaml has base == stable == main; a main→main
    # PR cannot exist, so nothing is misread as a promotion.
    assert orca_sync.discover_lanes("o/r", [_pr(head="main", base="main")], []) == []


def test_discover_lanes_queued_issue_has_no_branch_and_no_card() -> None:
    (lane,) = orca_sync.discover_lanes("o/r", [], [{"number": 11, "title": "do a thing"}])
    assert lane.kind == "queued"
    assert lane.branch is None
    assert lane.status == orca.STATUS_TODO
    assert lane.ensure_card is False
    assert "issue #11" in lane.comment


def test_discover_lanes_issue_already_in_flight_is_not_listed_twice() -> None:
    lanes = orca_sync.discover_lanes("o/r", [_pr(head="fix/issue-7")], [{"number": 7}])
    assert [lane.kind for lane in lanes] == ["implement"]


# --- converging lanes onto cards --------------------------------------------


def _repo(tmp_path: Path) -> orca.Repo:
    return orca.Repo(id="repo-1", path=tmp_path, slug="o/r")


def _card(path: Path, branch: str, comment: str = "", status: str | None = None) -> orca.Card:
    return orca.Card(path=path, branch=branch, comment=comment, workspace_status=status)


def _converge(
    lane: orca_sync.Lane, repo: orca.Repo, cards: dict[str, orca.Card], worktree_dir: str
) -> str:
    """Plan + write one lane, the way `sync_orca` does across a whole repo."""
    return orca_sync.write_card(orca_sync.plan_card(lane, repo, cards, worktree_dir))


def _lane(**over: Any) -> orca_sync.Lane:
    fields: dict[str, Any] = {
        "repo": "o/r",
        "kind": "implement",
        "label": "PR #42",
        "branch": "fix/issue-7",
        "comment": "implement lane · PR #42 · issue #7 → staging · checks passing",
        "status": orca.STATUS_IN_REVIEW,
        "card_name": "issue-7",
    }
    return orca_sync.Lane(**{**fields, **over})


Reported = list[tuple[Path, str | None, str | None]]


@pytest.fixture()
def reported(monkeypatch: pytest.MonkeyPatch) -> Reported:
    """Records every card write as (path, comment, status)."""
    calls: Reported = []

    def fake_report(path: Path, *, comment: str | None = None, status: str | None = None) -> bool:
        calls.append((path, comment, status))
        return True

    monkeypatch.setattr(orca_sync.orca, "report", fake_report)
    monkeypatch.setattr(orca_sync.orca, "await_indexed", lambda paths: True)
    return calls


def test_converge_adopts_the_existing_card_for_that_branch(
    tmp_path: Path, reported: Reported
) -> None:
    card = _card(tmp_path / ".worktrees" / "issue-7", "fix/issue-7", comment="stale")
    lane = _lane()

    line = _converge(lane, _repo(tmp_path), {"fix/issue-7": card}, ".worktrees")

    assert "card updated" in line
    assert reported == [(card.path, lane.comment, orca.STATUS_IN_REVIEW)]


def test_converge_is_a_no_op_when_the_card_already_says_it(
    tmp_path: Path, reported: Reported
) -> None:
    lane = _lane()
    card = _card(tmp_path / "wt", "fix/issue-7", comment=lane.comment, status=lane.status)

    line = _converge(lane, _repo(tmp_path), {"fix/issue-7": card}, ".worktrees")

    assert "already current" in line
    assert reported == []  # converged: a second run writes nothing


def test_converge_mirrors_the_pr_branch_when_no_card_tracks_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported: Reported,
) -> None:
    created: list[tuple[Path, str, str, str]] = []

    def fake_create(root: Path, wt_dir: str, name: str, branch: str) -> Path:
        created.append((root, wt_dir, name, branch))
        return root / wt_dir / name

    monkeypatch.setattr(orca_sync.worktree, "create_tracking", fake_create)

    line = _converge(_lane(), _repo(tmp_path), {}, ".worktrees")

    assert "card created" in line
    assert created == [(tmp_path, ".worktrees", "issue-7", "fix/issue-7")]
    assert reported == [
        (tmp_path / ".worktrees" / "issue-7", _lane().comment, orca.STATUS_IN_REVIEW)
    ]


def test_converge_never_creates_a_card_for_a_promotion_lane(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported: Reported,
) -> None:
    def boom(*args: object, **kwargs: object) -> Path:
        raise AssertionError("must not check out the working branch")

    monkeypatch.setattr(orca_sync.worktree, "create_tracking", boom)
    lane = _lane(kind="promote", branch="staging", card_name=None)

    line = _converge(lane, _repo(tmp_path), {}, ".worktrees")

    assert "reported only" in line
    assert reported == []


def test_converge_never_touches_a_card_for_a_queued_lane(
    tmp_path: Path, reported: Reported
) -> None:
    # A local `agent implement` run may own issue-7's card; the viewer must not
    # overwrite its live comment with "queued".
    card = _card(tmp_path / "wt", "fix/issue-7", comment="attempt 2/3: gates")
    lane = _lane(kind="queued", label="issue #7", branch=None, card_name=None)

    line = _converge(lane, _repo(tmp_path), {"fix/issue-7": card}, ".worktrees")

    assert "no card" in line
    assert reported == []


def test_converge_reports_a_mirror_failure_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported: Reported,
) -> None:
    def failing(*args: object, **kwargs: object) -> Path:
        raise CommandError("git worktree add failed: branch is checked out elsewhere")

    monkeypatch.setattr(orca_sync.worktree, "create_tracking", failing)

    line = _converge(_lane(), _repo(tmp_path), {}, ".worktrees")

    assert "could not mirror" in line
    assert "checked out elsewhere" in line
    assert reported == []


def test_converge_flags_a_card_orca_has_not_indexed_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orca_sync.orca, "report", lambda *a, **k: False)
    card = _card(tmp_path / "wt", "fix/issue-7", comment="stale")

    line = _converge(_lane(), _repo(tmp_path), {"fix/issue-7": card}, ".worktrees")

    assert "not indexed by Orca yet" in line


# --- the sync driver ---------------------------------------------------------


def test_sync_orca_is_a_clean_no_op_without_orca(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orca_sync.orca, "available", lambda: False)

    def boom(*args: object, **kwargs: object) -> Any:
        raise AssertionError("must not reach Orca or gh when Orca is unavailable")

    monkeypatch.setattr(orca_sync.orca, "repos", boom)
    monkeypatch.setattr(orca_sync, "open_prs", boom)

    lines: list[str] = []
    orca_sync.sync_orca(RegistryConfig(repos=["o/r"]), log=lines.append)

    assert len(lines) == 1
    assert "Orca is not running" in lines[0]


def test_sync_orca_skips_repos_orca_does_not_know(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orca_sync.orca, "available", lambda: True)
    monkeypatch.setattr(orca_sync.orca, "repos", list)

    lines: list[str] = []
    orca_sync.sync_orca(RegistryConfig(repos=["o/r"]), log=lines.append)

    assert any("not registered in Orca" in line for line in lines)


def test_sync_orca_syncs_each_registered_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reported: Reported,
) -> None:
    monkeypatch.setattr(orca_sync.orca, "available", lambda: True)
    monkeypatch.setattr(orca_sync.orca, "repos", lambda: [_repo(tmp_path)])
    monkeypatch.setattr(orca_sync.orca, "cards", lambda repo_id: [])
    monkeypatch.setattr(orca_sync, "open_prs", lambda repo: [_pr(rollup=[_check()])])
    monkeypatch.setattr(orca_sync, "ready_issues", lambda repo: [{"number": 11}])
    monkeypatch.setattr(
        orca_sync.worktree, "create_tracking", lambda root, wt_dir, name, branch: root / name
    )

    lines: list[str] = []
    orca_sync.sync_orca(RegistryConfig(repos=["o/r"]), log=lines.append)

    text = "\n".join(lines)
    assert "PR #42" in text and "card created" in text
    assert "issue #11" in text  # the queued lane is reported too
    assert reported == [(tmp_path / "issue-7", _lane().comment, orca.STATUS_IN_REVIEW)]


def test_sync_orca_checks_out_every_lane_before_writing_any_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Orca only notices new external worktrees on a rescan, so the checkouts
    # must all happen up front — later ones then overlap the earlier wait.
    order: list[str] = []
    monkeypatch.setattr(orca_sync.orca, "available", lambda: True)
    monkeypatch.setattr(orca_sync.orca, "repos", lambda: [_repo(tmp_path)])
    monkeypatch.setattr(orca_sync.orca, "cards", lambda repo_id: [])
    monkeypatch.setattr(orca_sync, "ready_issues", lambda repo: [])
    monkeypatch.setattr(
        orca_sync,
        "open_prs",
        lambda repo: [_pr(number=1, head="fix/issue-1"), _pr(number=2, head="fix/issue-2")],
    )

    def fake_create(root: Path, wt_dir: str, name: str, branch: str) -> Path:
        order.append(f"checkout {branch}")
        return root / name

    def fake_await(paths: list[Path]) -> bool:
        order.append(f"wait {[path.name for path in paths]}")
        return True

    monkeypatch.setattr(orca_sync.worktree, "create_tracking", fake_create)
    monkeypatch.setattr(orca_sync.orca, "await_indexed", fake_await)
    monkeypatch.setattr(
        orca_sync.orca,
        "report",
        lambda path, **kwargs: bool(order.append(f"card {path.name}")) or True,
    )

    orca_sync.sync_orca(RegistryConfig(repos=["o/r"]), log=lambda line: None)

    # One wait for the whole batch, after every checkout and before any write.
    assert order == [
        "checkout fix/issue-1",
        "checkout fix/issue-2",
        "wait ['issue-1', 'issue-2']",
        "card issue-1",
        "card issue-2",
    ]


def test_sync_orca_does_not_wait_when_every_card_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reported: Reported
) -> None:
    card = _card(tmp_path / "wt", "fix/issue-7", comment="stale")
    monkeypatch.setattr(orca_sync.orca, "available", lambda: True)
    monkeypatch.setattr(orca_sync.orca, "repos", lambda: [_repo(tmp_path)])
    monkeypatch.setattr(orca_sync.orca, "cards", lambda repo_id: [card])
    monkeypatch.setattr(orca_sync, "open_prs", lambda repo: [_pr(rollup=[_check()])])
    monkeypatch.setattr(orca_sync, "ready_issues", lambda repo: [])
    monkeypatch.setattr(
        orca_sync.orca,
        "await_indexed",
        lambda paths: pytest.fail("must not wait when nothing was checked out"),
    )

    orca_sync.sync_orca(RegistryConfig(repos=["o/r"]), log=lambda line: None)

    assert reported == [(card.path, _lane().comment, orca.STATUS_IN_REVIEW)]


def test_sync_orca_keeps_going_when_one_repo_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orca_sync.orca, "available", lambda: True)
    monkeypatch.setattr(orca_sync.orca, "repos", lambda: [_repo(tmp_path)])
    monkeypatch.setattr(orca_sync.orca, "cards", lambda repo_id: [])
    monkeypatch.setattr(orca_sync, "ready_issues", lambda repo: [])

    def failing(repo: str) -> list[dict[str, Any]]:
        raise CommandError("gh: could not resolve repository")

    monkeypatch.setattr(orca_sync, "open_prs", failing)

    lines: list[str] = []
    orca_sync.sync_orca(RegistryConfig(repos=["o/r"]), log=lines.append)

    assert any("could not read lane state" in line for line in lines)


# --- CLI wiring --------------------------------------------------------------


def test_status_sync_orca_flag_runs_the_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "load_registry", lambda: RegistryConfig(repos=["o/r"]))
    monkeypatch.setattr(orca_sync.orca, "available", lambda: False)

    result = runner.invoke(app, ["status", "--sync-orca"])

    assert result.exit_code == 0
    assert "Orca is not running" in result.output


def test_status_rejects_both_views_at_once() -> None:
    result = runner.invoke(app, ["status", "--sync-orca", "--pipelines"])

    assert result.exit_code == 1
    assert "one at a time" in result.stderr
