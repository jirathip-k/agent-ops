import json
import subprocess
from pathlib import Path

import pytest

from agent_ops import github
from agent_ops.utils import CommandError


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


def test_pr_references_issue_matches_body_mention() -> None:
    pr = {"headRefName": "some-branch", "title": "fix thing", "body": "Closes #132."}
    assert github.pr_references_issue(pr, 132)


def test_pr_references_issue_matches_title_mention() -> None:
    pr = {"headRefName": "some-branch", "title": "fix #132", "body": None}
    assert github.pr_references_issue(pr, 132)


def test_pr_references_issue_does_not_match_longer_number() -> None:
    pr = {"headRefName": "fix/issue-1321", "title": "fix #1321", "body": "Closes #1321"}
    assert not github.pr_references_issue(pr, 132)


def test_pr_references_issue_does_not_match_shorter_number() -> None:
    pr = {"headRefName": "fix/issue-13", "title": "fix #13", "body": "Closes #13"}
    assert not github.pr_references_issue(pr, 132)


def test_pr_references_issue_handles_missing_body() -> None:
    pr = {"headRefName": "some-branch", "title": "no mention here", "body": None}
    assert not github.pr_references_issue(pr, 132)


def test_open_prs_for_issue_returns_empty_when_gh_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise CommandError("gh: no remote configured")

    monkeypatch.setattr(github, "run", boom)
    assert github.open_prs_for_issue(132, cwd=tmp_path) == []


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
        },
        {
            "number": 141,
            "title": "unrelated",
            "body": None,
            "headRefName": "some-other-branch",
            "url": "https://github.com/org/repo/pull/141",
        },
    ]

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(prs))

    monkeypatch.setattr(github, "run", fake_run)
    result = github.open_prs_for_issue(132, cwd=tmp_path)
    assert [pr["number"] for pr in result] == [140]
