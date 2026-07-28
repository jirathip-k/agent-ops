import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agent_ops import github
from agent_ops.utils import SPAWN_FAILURE_RETURNCODE, TIMEOUT_RETURNCODE, CommandError, run


def test_get_issue_requests_comments(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"number": 1}))

    monkeypatch.setattr(github, "run", fake_run)
    github.get_issue(1, cwd=tmp_path)
    json_index = captured["cmd"].index("--json")
    assert "comments" in captured["cmd"][json_index + 1].split(",")


def test_pr_references_issue_matches_branch_name() -> None:
    pr = {"headRefName": "fix/issue-132", "title": "unrelated", "body": None}
    assert github.pr_references_issue(pr, 132)


def test_pr_references_issue_does_not_match_bare_body_mention() -> None:
    """A bare `#132` mention in the body (no real closing reference) must not block."""
    pr = {
        "headRefName": "some-branch",
        "title": "fix thing",
        "body": "Closes #132.",
        "closingIssuesReferences": [],
    }
    assert not github.pr_references_issue(pr, 132)


def test_pr_references_issue_does_not_match_bare_title_mention() -> None:
    """A bare `#132` mention in the title (no real closing reference) must not block."""
    pr = {
        "headRefName": "some-branch",
        "title": "fix #132",
        "body": None,
        "closingIssuesReferences": [],
    }
    assert not github.pr_references_issue(pr, 132)


def test_create_pr_default_has_no_draft_or_label_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="https://x/pull/1")

    monkeypatch.setattr(github, "run", fake_run)
    github.create_pr(tmp_path, base="main", title="t", body="b")

    assert captured["cmd"] == [
        "gh",
        "pr",
        "create",
        "--base",
        "main",
        "--title",
        "t",
        "--body",
        "b",
    ]


def test_create_pr_draft_and_labels_add_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="https://x/pull/1")

    monkeypatch.setattr(github, "run", fake_run)
    github.create_pr(
        tmp_path, base="main", title="t", body="b", draft=True, labels=("human-merge-only",)
    )

    assert captured["cmd"] == [
        "gh",
        "pr",
        "create",
        "--base",
        "main",
        "--title",
        "t",
        "--body",
        "b",
        "--draft",
        "--label",
        "human-merge-only",
    ]


def test_pr_references_issue_regression_sendmeter_bare_mention_does_not_block() -> None:
    """Regression fixture from #64: a cross-reference must not count as fixing it."""
    pr = {
        "headRefName": "fix/issue-197",
        "title": "sendmeter: something",
        "body": "the root cause is tracked separately in #194 and is out of scope here",
        "closingIssuesReferences": [{"number": 189}],
    }
    assert not github.pr_references_issue(pr, 194)


def test_pr_references_issue_regression_sendmeter_real_closing_reference_blocks() -> None:
    """The same PR's real closing reference (#189) must still block."""
    pr = {
        "headRefName": "fix/issue-197",
        "title": "sendmeter: something",
        "body": "the root cause is tracked separately in #194 and is out of scope here",
        "closingIssuesReferences": [{"number": 189}],
    }
    assert github.pr_references_issue(pr, 189)


def test_pr_references_issue_matches_closing_reference() -> None:
    pr = {
        "headRefName": "some-branch",
        "title": "unrelated title",
        "body": "unrelated body",
        "closingIssuesReferences": [{"number": 132}],
    }
    assert github.pr_references_issue(pr, 132)


def test_pr_references_issue_does_not_match_longer_number() -> None:
    pr = {
        "headRefName": "fix/issue-1321",
        "title": "fix #1321",
        "body": "Closes #1321",
        "closingIssuesReferences": [{"number": 1321}],
    }
    assert not github.pr_references_issue(pr, 132)


def test_pr_references_issue_does_not_match_shorter_number() -> None:
    pr = {
        "headRefName": "fix/issue-13",
        "title": "fix #13",
        "body": "Closes #13",
        "closingIssuesReferences": [{"number": 13}],
    }
    assert not github.pr_references_issue(pr, 132)


def test_pr_references_issue_handles_missing_closing_references_field() -> None:
    """A PR dict with no `closingIssuesReferences` key at all must not raise."""
    pr = {"headRefName": "some-branch", "title": "no mention here", "body": None}
    assert not github.pr_references_issue(pr, 132)


def test_open_prs_for_issue_returns_empty_when_gh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise CommandError("gh: no remote configured")

    monkeypatch.setattr(github, "run", boom)
    assert github.open_prs_for_issue(132, cwd=tmp_path) == []


def test_open_prs_for_issue_requests_closing_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, list[str]] = {}

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([]))

    monkeypatch.setattr(github, "run", fake_run)
    github.open_prs_for_issue(132, cwd=tmp_path)
    json_index = captured["cmd"].index("--json")
    assert "closingIssuesReferences" in captured["cmd"][json_index + 1].split(",")


def test_open_prs_for_issue_filters_to_matching_prs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prs = [
        {
            "number": 140,
            "title": "unrelated",
            "body": None,
            "headRefName": "fix/issue-132",
            "url": "https://github.com/org/repo/pull/140",
            "closingIssuesReferences": [],
        },
        {
            "number": 141,
            "title": "unrelated",
            "body": None,
            "headRefName": "some-other-branch",
            "url": "https://github.com/org/repo/pull/141",
            "closingIssuesReferences": [],
        },
        {
            "number": 142,
            "title": "unrelated",
            "body": "mentions #132 in passing, out of scope",
            "headRefName": "some-other-branch-2",
            "url": "https://github.com/org/repo/pull/142",
            "closingIssuesReferences": [{"number": 132}],
        },
    ]

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(prs))

    monkeypatch.setattr(github, "run", fake_run)
    result = github.open_prs_for_issue(132, cwd=tmp_path)
    assert [pr["number"] for pr in result] == [140, 142]


# --- `--pr` is a URL, or it is refused (issue #122) ------------------------


def _repo_with_remote(tmp_path: Path, url: str) -> Path:
    run(["git", "init", "-b", "main"], cwd=tmp_path)
    run(["git", "remote", "add", "origin", url], cwd=tmp_path)
    return tmp_path


def test_normalise_pr_url_expands_a_bare_number(tmp_path: Path) -> None:
    """`--pr 121` stored `{"pr_url": "121"}`, which `agent runs` rendered as
    `PR #121 — 121` — a field named `_url` holding something that isn't one."""
    repo = _repo_with_remote(tmp_path, "git@github.com:jirathip-k/agent-ops.git")

    assert (
        github.normalise_pr_url("121", repo) == "https://github.com/jirathip-k/agent-ops/pull/121"
    )
    # ...and the `#121` an agent is equally likely to type.
    assert github.normalise_pr_url("#121", repo).endswith("/pull/121")


def test_normalise_pr_url_reads_an_https_remote_too(tmp_path: Path) -> None:
    repo = _repo_with_remote(tmp_path, "https://github.com/jirathip-k/agent-ops.git")
    assert github.normalise_pr_url("121", repo).endswith("jirathip-k/agent-ops/pull/121")


def test_normalise_pr_url_passes_a_real_url_through(tmp_path: Path) -> None:
    url = "https://github.com/jirathip-k/agent-ops/pull/121"
    assert github.normalise_pr_url(url, tmp_path) == url
    assert github.normalise_pr_url(url + "/", tmp_path) == url


def test_normalise_pr_url_refuses_what_it_cannot_make_a_url_of(tmp_path: Path) -> None:
    for value in ("soon", "https://github.com/jirathip-k/agent-ops", "  "):
        with pytest.raises(CommandError):
            github.normalise_pr_url(value, tmp_path)


def test_normalise_pr_url_says_so_when_a_number_cannot_be_expanded(tmp_path: Path) -> None:
    """No remote to derive the URL from — better to refuse at the boundary than
    to store the number and render it as a URL later."""
    run(["git", "init", "-b", "main"], cwd=tmp_path)

    with pytest.raises(CommandError, match="pass the full URL"):
        github.normalise_pr_url("121", tmp_path)


def test_remote_slug_is_none_outside_a_repo(tmp_path: Path) -> None:
    assert github.remote_slug(tmp_path) is None


def _label_run(
    monkeypatch: pytest.MonkeyPatch,
    existing: list[dict[str, str]],
    fails: frozenset[str] = frozenset(),
) -> list[list[str]]:
    """Stub `github.run` for label-sync tests; returns the argv list it recorded."""
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["gh", "label", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(existing), stderr="")
        if cmd[:3] == ["gh", "label", "create"] and cmd[3] in fails:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="HTTP 403: Resource not accessible by integration\nmore"
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(github, "run", fake_run)
    return calls


def test_sync_labels_creates_all_on_a_fresh_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _label_run(monkeypatch, existing=[])
    labels = {"agent-ready": github.Label("1d76db", "Groomed and safe to implement")}

    result = github.sync_labels(tmp_path, labels)

    assert result == github.LabelSync(created=["agent-ready"], updated=[], unchanged=[], failed=[])
    (create_call,) = [c for c in calls if c[:3] == ["gh", "label", "create"]]
    assert "--description" in create_call
    assert "Groomed and safe to implement" in create_call


def test_sync_labels_is_a_noop_when_everything_already_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _label_run(
        monkeypatch,
        existing=[{"name": "agent-ready", "color": "1D76DB", "description": "Groomed and safe"}],
    )
    labels = {"agent-ready": github.Label("1d76db", "Groomed and safe")}

    result = github.sync_labels(tmp_path, labels)

    assert result == github.LabelSync(created=[], updated=[], unchanged=["agent-ready"], failed=[])
    assert not any(c[:3] == ["gh", "label", "create"] for c in calls)


def test_sync_labels_treats_hash_prefix_and_case_as_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _label_run(
        monkeypatch, existing=[{"name": "agent-ready", "color": "1d76db", "description": ""}]
    )

    result = github.sync_labels(tmp_path, {"agent-ready": github.Label("#1D76DB")})

    assert result.unchanged == ["agent-ready"]
    assert not any(c[:3] == ["gh", "label", "create"] for c in calls)


def test_sync_labels_updates_a_drifted_color(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _label_run(
        monkeypatch,
        existing=[{"name": "agent-ready", "color": "ff0000", "description": "Groomed and safe"}],
    )
    labels = {"agent-ready": github.Label("1d76db", "Groomed and safe")}

    result = github.sync_labels(tmp_path, labels)

    assert result == github.LabelSync(created=[], updated=["agent-ready"], unchanged=[], failed=[])


def test_sync_labels_updates_a_drifted_description(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _label_run(
        monkeypatch,
        existing=[{"name": "agent-ready", "color": "1d76db", "description": "old text"}],
    )
    labels = {"agent-ready": github.Label("1d76db", "new text")}

    result = github.sync_labels(tmp_path, labels)

    assert result.updated == ["agent-ready"]


def test_sync_labels_one_failure_does_not_abort_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _label_run(monkeypatch, existing=[], fails=frozenset({"blocked"}))
    labels = {
        "blocked": github.Label("b60205", "desc"),
        "agent-ready": github.Label("1d76db", "desc2"),
    }

    result = github.sync_labels(tmp_path, labels)

    assert result.created == ["agent-ready"]
    assert result.failed == [("blocked", "HTTP 403: Resource not accessible by integration")]


def test_sync_labels_survives_a_wedged_label_on_one_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`check=False` never raises (agent-ops#154) — a hang on one label comes
    back as a synthetic non-zero `CompletedProcess`, which must not propagate
    out of the loop and cost the rest."""
    timeout_message = "`gh label create blocked` did not finish within 120s"

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "label", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([]), stderr="")
        if cmd[:3] == ["gh", "label", "create"] and cmd[3] == "blocked":
            return subprocess.CompletedProcess(
                cmd, TIMEOUT_RETURNCODE, stdout="", stderr=timeout_message
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(github, "run", fake_run)
    labels = {
        "blocked": github.Label("b60205", "desc"),
        "agent-ready": github.Label("1d76db", "desc2"),
    }

    result = github.sync_labels(tmp_path, labels)

    assert result.created == ["agent-ready"]
    assert result.failed == [("blocked", timeout_message)]


def test_sync_labels_pins_the_repo_when_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _label_run(monkeypatch, existing=[])
    labels = {"agent-ready": github.Label("1d76db", "desc")}

    github.sync_labels(tmp_path, labels, repo="acme/widgets")

    list_call = next(c for c in calls if c[:3] == ["gh", "label", "list"])
    create_call = next(c for c in calls if c[:3] == ["gh", "label", "create"])
    assert list_call[-2:] == ["--repo", "acme/widgets"]
    assert create_call[-2:] == ["--repo", "acme/widgets"]


def test_sync_labels_omits_repo_flag_when_not_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _label_run(monkeypatch, existing=[])
    labels = {"agent-ready": github.Label("1d76db", "desc")}

    github.sync_labels(tmp_path, labels)

    for call in calls:
        assert "--repo" not in call


def test_sync_labels_raises_when_label_list_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "label", "list"]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="HTTP 401: Bad credentials"
            )
        raise AssertionError(f"unexpected call: {cmd}")

    monkeypatch.setattr(github, "run", fake_run)

    with pytest.raises(CommandError, match="Bad credentials"):
        github.sync_labels(tmp_path, {"agent-ready": github.Label("1d76db")})


def test_sync_labels_raises_command_error_when_label_list_is_not_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`gh label list` can exit 0 with non-JSON stdout (e.g. a stale `gh`
    login/setup notice). `json.JSONDecodeError` is a `ValueError`, which none
    of sync_labels' four callers catch — so this must surface as the same
    `CommandError` the returncode-!=-0 branch above raises, not a bare
    ValueError, or a completed triage/groom/scout run crashes at this step."""

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["gh", "label", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="Welcome to gh!\n", stderr="")
        raise AssertionError(f"unexpected call: {cmd}")

    monkeypatch.setattr(github, "run", fake_run)

    with pytest.raises(CommandError, match="could not parse"):
        github.sync_labels(tmp_path, {"agent-ready": github.Label("1d76db")})


# --- `observed_ci` — giving a resumed run its PR's failing CI (#236) --------


def _rollup_check(
    name: str = "test", conclusion: str = "SUCCESS", status: str = "COMPLETED", url: str = ""
) -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "workflowName": "CI",
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": url,
    }


def _pr_view_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: str = "OPEN",
    number: int = 7,
    url: str = "https://github.com/o/r/pull/7",
    rollup: list[dict[str, Any]] | None = None,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        payload = {
            "number": number,
            "url": url,
            "state": state,
            "statusCheckRollup": [] if rollup is None else rollup,
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(github, "run", fake_run)
    return calls


def test_observed_ci_reports_failing_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rollup = [
        _rollup_check(name="pytest", conclusion="SUCCESS"),
        _rollup_check(
            name="actionlint",
            conclusion="FAILURE",
            url="https://github.com/o/r/actions/runs/1/job/11",
        ),
    ]
    _pr_view_stub(monkeypatch, rollup=rollup)
    monkeypatch.setattr(github, "_failed_job_log", lambda checks, cwd: None)

    ci = github.observed_ci("fix/issue-7", cwd=tmp_path)

    assert ci.state == "failing"
    assert ci.pr_number == 7
    assert ci.pr_url == "https://github.com/o/r/pull/7"
    assert [c.name for c in ci.failing] == ["actionlint"]
    assert ci.failing[0].workflow == "CI"
    assert ci.failing[0].url == "https://github.com/o/r/actions/runs/1/job/11"


def test_observed_ci_mixed_failing_and_pending_reports_only_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rollup = [
        _rollup_check(name="pytest", status="IN_PROGRESS", conclusion=""),
        _rollup_check(name="actionlint", conclusion="FAILURE"),
    ]
    _pr_view_stub(monkeypatch, rollup=rollup)
    monkeypatch.setattr(github, "_failed_job_log", lambda checks, cwd: None)

    ci = github.observed_ci("fix/issue-7", cwd=tmp_path)

    assert ci.state == "failing"
    assert [c.name for c in ci.failing] == ["actionlint"]


@pytest.mark.parametrize(
    ("rollup", "expected"),
    [
        ([_rollup_check()], "passing"),
        ([_rollup_check(status="IN_PROGRESS", conclusion="")], "pending"),
        ([], "none"),
    ],
)
def test_observed_ci_non_failing_states_carry_no_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rollup: list[dict[str, Any]],
    expected: str,
) -> None:
    _pr_view_stub(monkeypatch, rollup=rollup)

    ci = github.observed_ci("fix/issue-7", cwd=tmp_path)

    assert ci.state == expected
    assert ci.failing == ()
    assert ci.log_excerpt is None


def test_observed_ci_no_pr_found_is_reported_as_no_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr='no pull requests found for branch "fix/issue-7"'
        )

    monkeypatch.setattr(github, "run", fake_run)

    ci = github.observed_ci("fix/issue-7", cwd=tmp_path)

    assert ci.state == "no-pr"


def test_observed_ci_other_gh_failure_is_unknown_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 500: server error")

    monkeypatch.setattr(github, "run", fake_run)

    ci = github.observed_ci("fix/issue-7", cwd=tmp_path)

    assert ci.state == "unknown"


def test_observed_ci_spawn_failure_is_unknown_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd, SPAWN_FAILURE_RETURNCODE, stdout="", stderr="`gh` could not be started"
        )

    monkeypatch.setattr(github, "run", fake_run)

    ci = github.observed_ci("fix/issue-7", cwd=tmp_path)

    assert ci.state == "unknown"


def test_observed_ci_unparseable_json_is_unknown_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="Welcome to gh!\n", stderr="")

    monkeypatch.setattr(github, "run", fake_run)

    ci = github.observed_ci("fix/issue-7", cwd=tmp_path)

    assert ci.state == "unknown"


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_observed_ci_closed_or_merged_pr_is_no_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    _pr_view_stub(monkeypatch, state=state, rollup=[_rollup_check(conclusion="FAILURE")])

    ci = github.observed_ci("fix/issue-7", cwd=tmp_path)

    assert ci.state == "no-pr"


# --- `_failed_job_log` ------------------------------------------------------


def _failing_check(url: str, name: str = "actionlint") -> github.FailingCheck:
    return github.FailingCheck(name=name, workflow="CI", url=url)


def test_failed_job_log_fetches_the_job_id_parsed_from_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="failure: exit 1", stderr="")

    monkeypatch.setattr(github, "run", fake_run)
    checks = [_failing_check("https://github.com/o/r/actions/runs/123/job/456")]

    excerpt = github._failed_job_log(checks, tmp_path)

    assert excerpt == "failure: exit 1"
    assert calls == [["gh", "run", "view", "--job", "456", "--log-failed"]]


def test_failed_job_log_truncates_to_the_tail_and_char_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_log = "\n".join(f"line {i}" for i in range(500))

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=long_log, stderr="")

    monkeypatch.setattr(github, "run", fake_run)
    checks = [_failing_check("https://github.com/o/r/actions/runs/1/job/1")]

    excerpt = github._failed_job_log(checks, tmp_path)

    assert excerpt is not None
    assert len(excerpt) <= github._LOG_CHAR_CAP
    assert excerpt.count("\n") + 1 <= github._LOG_TAIL_LINES
    assert "line 499" in excerpt
    assert "line 0" not in excerpt


def test_failed_job_log_at_most_two_jobs_are_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="log", stderr="")

    monkeypatch.setattr(github, "run", fake_run)
    checks = [
        _failing_check(f"https://github.com/o/r/actions/runs/{i}/job/{i}", name=f"check{i}")
        for i in range(1, 5)
    ]

    github._failed_job_log(checks, tmp_path)

    assert len(calls) == 2


def test_failed_job_log_failing_fetch_returns_partial_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[4] == "456":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(cmd, 0, stdout="good log", stderr="")

    monkeypatch.setattr(github, "run", fake_run)
    checks = [
        _failing_check("https://github.com/o/r/actions/runs/1/job/456", name="a"),
        _failing_check("https://github.com/o/r/actions/runs/1/job/789", name="b"),
    ]

    excerpt = github._failed_job_log(checks, tmp_path)

    assert excerpt == "good log"


def test_failed_job_log_all_fetches_fail_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="rate limited")

    monkeypatch.setattr(github, "run", fake_run)
    checks = [_failing_check("https://github.com/o/r/actions/runs/1/job/456")]

    excerpt = github._failed_job_log(checks, tmp_path)

    assert excerpt is None


def test_failed_job_log_no_job_url_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("must not call gh when no check has a parseable job id")

    monkeypatch.setattr(github, "run", boom)
    checks = [_failing_check("https://github.com/o/r/checks/999")]

    excerpt = github._failed_job_log(checks, tmp_path)

    assert excerpt is None
