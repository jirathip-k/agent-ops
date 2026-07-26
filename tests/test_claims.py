from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_ops import claims, cli, github, runs, surfaces, worktree
from agent_ops.loop import LoopOutcome
from agent_ops.utils import CommandError
from agent_ops.utils import run as utils_run
from agent_ops.workflows import implement as implement_module
from agent_ops.workflows.implement import SelfReview, run_implement, run_resume
from agent_ops.workflows.spawn import report_outcome

runner = CliRunner()


class _FakeGh:
    """Records every `gh` invocation and answers from a scripted table.

    Keyed on a short prefix of the argv (`gh issue edit`, `gh api`, ...) so a
    test states only the calls it cares about; anything unscripted succeeds
    with empty output, which is what the real `gh` does for the label-create
    call every claim makes.
    """

    def __init__(self, answers: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(
        self, cmd: list[str], *, cwd: Path | None = None, check: bool = True, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(cmd)
        for prefix, (code, out, err) in self.answers.items():
            if " ".join(cmd).startswith(prefix):
                if check and code != 0:
                    raise CommandError(f"`{' '.join(cmd)}` failed with exit code {code}")
                return subprocess.CompletedProcess(cmd, code, out, err)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def matching(self, prefix: str) -> list[list[str]]:
        return [call for call in self.calls if " ".join(call).startswith(prefix)]


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root that looks like it has a GitHub remote."""
    monkeypatch.setattr(github, "remote_slug", lambda cwd: "acme/widget")
    return tmp_path


def _stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# --- claim / release -------------------------------------------------------


def test_claim_creates_the_label_then_applies_it(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = _FakeGh()
    monkeypatch.setattr(claims, "run", gh)

    assert claims.claim(repo, 131) is True

    created = gh.matching("gh label create")
    assert created and claims.CLAIM_LABEL in created[0]
    # --force, so a repo onboarded before this label existed still claims
    # rather than failing every run on "label not found".
    assert "--force" in created[0]
    assert gh.matching(f"gh issue edit 131 --add-label {claims.CLAIM_LABEL}")


def test_claim_is_a_no_op_without_an_origin_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "remote_slug", lambda cwd: None)
    gh = _FakeGh()
    monkeypatch.setattr(claims, "run", gh)

    log: list[str] = []
    assert claims.claim(tmp_path, 7, log=log.append) is False

    assert gh.calls == []
    assert any("origin" in line for line in log)


def test_claim_never_raises_when_gh_fails(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = _FakeGh({"gh issue edit": (1, "", "HTTP 403: Resource not accessible")})
    monkeypatch.setattr(claims, "run", gh)

    log: list[str] = []
    assert claims.claim(repo, 131, log=log.append) is False
    assert any("could not claim #131" in line for line in log)


def test_claim_never_raises_when_gh_is_missing(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_gh(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("gh")

    monkeypatch.setattr(claims, "run", no_gh)

    log: list[str] = []
    assert claims.claim(repo, 131, log=log.append) is False
    assert any("could not claim #131" in line for line in log)


def test_release_removes_the_label(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = _FakeGh()
    monkeypatch.setattr(claims, "run", gh)

    assert claims.release(repo, 131) is True
    assert gh.matching(f"gh issue edit 131 --remove-label {claims.CLAIM_LABEL}")


def test_release_never_raises_when_gh_fails(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = _FakeGh({"gh issue edit": (1, "", "'agent:claimed' not found")})
    monkeypatch.setattr(claims, "run", gh)

    log: list[str] = []
    assert claims.release(repo, 131, log=log.append) is False
    assert any("could not clear the claim on #131" in line for line in log)


def test_claim_object_releases_only_what_it_took(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = _FakeGh()
    monkeypatch.setattr(claims, "run", gh)

    claim = claims.Claim(repo, 131)
    # Never taken (the run bailed out before starting): the `finally` must not
    # fire an API call, let alone strip a claim another run is holding.
    assert claim.release() is False
    assert gh.matching("gh issue edit") == []

    claim.take()
    assert claim.release() is True
    assert len(gh.matching("gh issue edit 131 --remove-label")) == 1


def test_claim_object_release_keeps_the_claim_when_gh_fails(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = _FakeGh({"gh issue edit 131 --remove-label": (1, "", "HTTP 500")})
    monkeypatch.setattr(claims, "run", gh)

    claim = claims.Claim(repo, 131)
    claim.take()
    assert claim.release() is False
    # Still held as far as this process knows — which is what the doctor check
    # for "recorded an outcome but never released" exists to catch.
    assert claim.taken is True


# --- reading the claim's age off GitHub ------------------------------------


def test_claimed_at_reads_the_most_recent_labeled_event(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = _FakeGh(
        {"gh api": (0, "2026-07-20T04:13:04Z\n2026-07-26T04:13:04Z\n", "")},
    )
    monkeypatch.setattr(claims, "run", gh)

    applied = claims.claimed_at(repo, 131)

    assert applied == _epoch("2026-07-26T04:13:04Z")
    call = " ".join(gh.matching("gh api")[0])
    assert "issues/131/events" in call and claims.CLAIM_LABEL in call


def test_claimed_at_is_none_when_the_issue_was_never_claimed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "run", _FakeGh({"gh api": (0, "", "")}))
    assert claims.claimed_at(repo, 131) is None


def test_claimed_at_is_none_when_gh_cannot_answer(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "run", _FakeGh({"gh api": (1, "", "HTTP 404")}))
    assert claims.claimed_at(repo, 131) is None


def test_claimed_at_is_none_for_an_unparseable_timestamp(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claims, "run", _FakeGh({"gh api": (0, "last tuesday\n", "")}))
    assert claims.claimed_at(repo, 131) is None


def _epoch(stamp: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(stamp).timestamp()


# --- the stale-claim audit -------------------------------------------------


def _write_outcome(repo: Path, issue: int, *, state: str, finished_at: float) -> None:
    """An outcome record with a chosen `finished_at`, since the audit compares it
    against the claim's age and the tests fix both ends of that comparison."""
    runs.write_outcome(repo, issue, state=state)
    path = runs.outcome_path(repo, issue)
    payload = json.loads(path.read_text())
    payload["finished_at"] = finished_at
    path.write_text(json.dumps(payload))


def _audit_gh(claimed: list[int], applied: dict[int, float]) -> dict[str, tuple[int, str, str]]:
    answers: dict[str, tuple[int, str, str]] = {
        "gh issue list": (0, json.dumps([{"number": n} for n in claimed]), ""),
    }
    for issue, epoch in applied.items():
        answers[f"gh api repos/{{owner}}/{{repo}}/issues/{issue}/events"] = (
            0,
            _stamp(epoch) + "\n",
            "",
        )
    return answers


def test_audit_reports_a_claim_past_the_ttl(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(claims, "run", _FakeGh(_audit_gh([131], {131: now - 11 * 3600})))
    monkeypatch.setattr(worktree, "list_worktrees", lambda root: [])

    audit = claims.audit(repo, now=now)

    assert [stale.issue for stale in audit.stale] == [131]
    assert "11h" in audit.stale[0].detail
    assert "TTL" in audit.stale[0].detail


def test_audit_leaves_a_fresh_claim_alone(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(claims, "run", _FakeGh(_audit_gh([131], {131: now - 600})))
    monkeypatch.setattr(worktree, "list_worktrees", lambda root: [])

    audit = claims.audit(repo, now=now)

    assert audit.stale == []
    assert audit.claimed == [131]


def test_audit_reports_a_release_that_did_not_happen(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stale shape a TTL still hides for hours: the run finished, the
    `gh` call clearing the label failed, and the issue is blocked with a local
    record proving nothing is working on it."""
    now = 1_800_000_000.0
    monkeypatch.setattr(claims, "run", _FakeGh(_audit_gh([131], {131: now - 900})))
    monkeypatch.setattr(worktree, "list_worktrees", lambda root: [])
    _write_outcome(repo, 131, state="done", finished_at=now - 60)

    audit = claims.audit(repo, now=now)

    assert [stale.issue for stale in audit.stale] == [131]
    assert "`done`" in audit.stale[0].detail


def test_audit_ignores_an_outcome_record_from_an_earlier_cycle(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record that predates the claim belongs to a previous run on the same
    issue and says nothing about this one — accusing on it would report a
    healthy live run as stale, which is the failure this whole check exists to
    avoid producing."""
    now = 1_800_000_000.0
    monkeypatch.setattr(claims, "run", _FakeGh(_audit_gh([131], {131: now - 300})))
    monkeypatch.setattr(worktree, "list_worktrees", lambda root: [])
    _write_outcome(repo, 131, state="done", finished_at=now - 4000)  # before the claim

    assert claims.audit(repo, now=now).stale == []


def test_audit_does_not_accuse_a_claim_it_cannot_date(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    answers = _audit_gh([131], {})
    answers["gh api"] = (1, "", "HTTP 404")
    monkeypatch.setattr(claims, "run", _FakeGh(answers))
    monkeypatch.setattr(worktree, "list_worktrees", lambda root: [])
    _write_outcome(repo, 131, state="done", finished_at=now - 60)

    assert claims.audit(repo, now=now).stale == []


def test_audit_does_not_treat_a_foreign_claim_as_stale(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No worktree and no outcome record here means another machine holds it,
    not that the run is dead. 'I can't see it' is never evidence of death."""
    now = 1_800_000_000.0
    monkeypatch.setattr(claims, "run", _FakeGh(_audit_gh([131], {131: now - 60})))
    monkeypatch.setattr(worktree, "list_worktrees", lambda root: [])

    assert claims.audit(repo, now=now).stale == []


def test_audit_reports_a_local_worktree_nothing_has_claimed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The #126 shape: an agent started by hand, and the CI lane free to start
    its own run on the same issue."""
    now = 1_800_000_000.0
    monkeypatch.setattr(claims, "run", _FakeGh(_audit_gh([], {})))
    monkeypatch.setattr(
        worktree,
        "list_worktrees",
        lambda root: [
            worktree.Worktree(repo / ".worktrees" / "issue-116", "fix/issue-116"),
            worktree.Worktree(repo, "main"),
        ],
    )

    audit = claims.audit(repo, now=now)

    assert audit.unclaimed == [116]


def test_audit_does_not_report_a_worktree_that_is_claimed(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000.0
    monkeypatch.setattr(claims, "run", _FakeGh(_audit_gh([116], {116: now - 60})))
    monkeypatch.setattr(
        worktree,
        "list_worktrees",
        lambda root: [worktree.Worktree(repo / ".worktrees" / "issue-116", "fix/issue-116")],
    )

    assert claims.audit(repo, now=now).unclaimed == []


def test_fmt_age_reads_as_a_duration() -> None:
    assert claims._fmt_age(11 * 3600) == "11h"
    assert claims._fmt_age(90 * 60) == "1h"
    assert claims._fmt_age(45 * 60) == "45m"
    assert claims._fmt_age(30) == "<1m"


# --- what an operator is actually told -------------------------------------


def test_doctor_names_a_stale_claim_and_how_to_clear_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.github, "remote_slug", lambda cwd: "acme/widget")
    monkeypatch.setattr(
        cli.claims,
        "audit",
        lambda root: claims.ClaimAudit(
            stale=[claims.StaleClaim(131, "#131 has been claimed for 11h")],
            unclaimed=[],
            claimed=[131],
        ),
    )

    cli._report_claims(tmp_path)

    out = capsys.readouterr().out
    assert "#131 has been claimed for 11h" in out
    assert "agent claim 131 --release" in out


def test_doctor_names_local_work_nothing_has_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The hand-started path this cannot cover automatically — made visible
    rather than left silent."""
    monkeypatch.setattr(cli.github, "remote_slug", lambda cwd: "acme/widget")
    monkeypatch.setattr(
        cli.claims,
        "audit",
        lambda root: claims.ClaimAudit(stale=[], unclaimed=[116], claimed=[]),
    )

    cli._report_claims(tmp_path)

    out = capsys.readouterr().out
    assert claims.CLAIM_LABEL in out
    assert "agent claim 116" in out


def test_doctor_reports_an_unreadable_claim_check_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.github, "remote_slug", lambda cwd: "acme/widget")

    def boom(root: Path) -> claims.ClaimAudit:
        raise CommandError("gh issue list failed: HTTP 403")

    monkeypatch.setattr(cli.claims, "audit", boom)

    cli._report_claims(tmp_path)

    assert "could not check agent claims" in capsys.readouterr().out


def test_doctor_says_nothing_about_claims_in_a_repo_with_no_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli.github, "remote_slug", lambda cwd: None)

    def never(root: Path) -> claims.ClaimAudit:
        raise AssertionError("a repo with no remote has nothing to reconcile")

    monkeypatch.setattr(cli.claims, "audit", never)

    cli._report_claims(tmp_path)

    assert capsys.readouterr().out == ""


def test_doctor_surfaces_a_stale_claim_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wired in for real, through `agent doctor` — a warning, never a failure:
    a stale claim is somebody else's mistake to clear, not a broken install."""
    import shutil

    monkeypatch.setattr(shutil, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(cli.github, "remote_slug", lambda cwd: "acme/widget")
    monkeypatch.setattr(
        cli.claims,
        "audit",
        lambda root: claims.ClaimAudit(
            stale=[claims.StaleClaim(131, "#131 has been claimed for 11h")],
            unclaimed=[],
            claimed=[131],
        ),
    )
    runner.invoke(cli.app, ["init", "--project", str(tmp_path)])

    result = runner.invoke(cli.app, ["doctor", "--project", str(tmp_path)])

    assert result.exit_code == 0
    assert "#131 has been claimed for 11h" in result.output


def test_init_lists_the_claim_label_for_a_repo_to_create(tmp_path: Path) -> None:
    """`agent claim` from a hand-started worktree fails on a repo where the
    label does not exist, and a failed claim is exactly the silence this is
    supposed to remove."""
    result = runner.invoke(cli.app, ["init", "--project", str(tmp_path)])

    assert f"gh label create {claims.CLAIM_LABEL}" in result.output


def test_agent_claim_takes_and_releases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    taken: list[tuple[str, int]] = []
    monkeypatch.setattr(
        cli.claims, "claim", lambda root, issue, **kw: taken.append(("claim", issue)) or True
    )
    monkeypatch.setattr(
        cli.claims, "release", lambda root, issue, **kw: taken.append(("release", issue)) or True
    )

    assert runner.invoke(cli.app, ["claim", "131", "--project", str(tmp_path)]).exit_code == 0
    assert (
        runner.invoke(cli.app, ["claim", "131", "--release", "--project", str(tmp_path)]).exit_code
        == 0
    )

    assert taken == [("claim", 131), ("release", 131)]


def test_agent_claim_exits_nonzero_when_the_claim_did_not_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike the automatic paths, a claim somebody asked for by hand is loud
    when it fails: silently reporting success would leave them thinking the CI
    lane is holding off when it is not."""
    monkeypatch.setattr(cli.claims, "claim", lambda root, issue, **kw: False)

    result = runner.invoke(cli.app, ["claim", "131", "--project", str(tmp_path)])

    assert result.exit_code == 1


# --- lane wiring: who claims, and every way a claim is given back ----------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    utils_run(["git", "init", "-b", "main"], cwd=tmp_path)
    utils_run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path)
    utils_run(["git", "config", "user.name", "test"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("hello\n")
    utils_run(["git", "add", "."], cwd=tmp_path)
    utils_run(["git", "commit", "-m", "init"], cwd=tmp_path)
    return tmp_path


class _Ledger:
    """Records claim/release calls in order, in place of the `gh` round trips."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple[str, int]] = []
        monkeypatch.setattr(claims, "claim", self._claim)
        monkeypatch.setattr(claims, "release", self._release)

    def _claim(self, project_root: Path, issue: int, **kwargs: object) -> bool:
        self.events.append(("claim", issue))
        return True

    def _release(self, project_root: Path, issue: int, **kwargs: object) -> bool:
        self.events.append(("release", issue))
        return True


def _fake_issue(number: int, cwd: Path) -> dict[str, object]:
    return {"number": number, "title": "some bug", "body": "body", "labels": []}


def _implement_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_ops.config import ProjectConfig
    from agent_ops.runtimes.base import RunRequest

    def fake_role_request(
        config: ProjectConfig,
        role_name: str,
        prompt: str,
        cwd: Path,
        *,
        runtime_override: str | None = None,
        extra_allowed_tools: tuple[str, ...] = (),
    ) -> tuple[object, RunRequest]:
        return object(), RunRequest(prompt=prompt, cwd=cwd)

    monkeypatch.setattr(github, "get_issue", _fake_issue)
    monkeypatch.setattr(github, "open_prs_for_issue", lambda number, cwd: [])
    monkeypatch.setattr(implement_module, "role_request", fake_role_request)


def test_run_implement_claims_the_issue_and_gives_it_back_on_a_halt(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The self-review halt keeps the worktree, so nothing else would have
    released it — and a halt is waiting on a human, not occupancy."""
    ledger = _Ledger(monkeypatch)
    _implement_stubs(monkeypatch)
    monkeypatch.setattr(
        implement_module, "run_task_loop", lambda *a, **k: LoopOutcome(True, 1, None, [])
    )
    monkeypatch.setattr(
        implement_module, "_self_review", lambda *a, **k: SelfReview(False, "found issues")
    )
    monkeypatch.setattr(github, "comment_on_issue", lambda number, body, cwd: None)
    plan_file = git_repo / "plan.md"
    plan_file.write_text("approved plan")

    ok = run_implement(git_repo, 131, plan_file=plan_file, log=lambda _: None)

    assert ok is False
    assert ledger.events == [("claim", 131), ("release", 131)]


def test_run_implement_gives_the_claim_back_when_gates_never_pass(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _Ledger(monkeypatch)
    _implement_stubs(monkeypatch)
    monkeypatch.setattr(
        implement_module, "run_task_loop", lambda *a, **k: LoopOutcome(False, 3, None, [])
    )
    plan_file = git_repo / "plan.md"
    plan_file.write_text("approved plan")

    assert run_implement(git_repo, 131, plan_file=plan_file, log=lambda _: None) is False
    assert ledger.events == [("claim", 131), ("release", 131)]


def test_run_implement_gives_the_claim_back_when_the_run_raises(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The path no per-exit release would have covered: a run that dies part
    way through still hands the issue back rather than stranding it until the
    TTL. This is the whole reason the release is a `finally`."""
    ledger = _Ledger(monkeypatch)
    _implement_stubs(monkeypatch)

    def explode(*args: object, **kwargs: object) -> LoopOutcome:
        raise CommandError("the runtime died mid-loop")

    monkeypatch.setattr(implement_module, "run_task_loop", explode)
    plan_file = git_repo / "plan.md"
    plan_file.write_text("approved plan")

    with pytest.raises(CommandError):
        run_implement(git_repo, 131, plan_file=plan_file, log=lambda _: None)

    assert ledger.events == [("claim", 131), ("release", 131)]


def test_run_implement_does_not_claim_when_it_refuses_to_start(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The already-has-an-open-PR bail-out starts nothing, so it must not take
    a claim — nor strip one a *different* run is holding on its way out."""
    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(github, "get_issue", _fake_issue)
    monkeypatch.setattr(
        github,
        "open_prs_for_issue",
        lambda number, cwd: [{"number": 9, "url": "https://x/pull/9", "headRefName": ""}],
    )

    assert run_implement(git_repo, 131, log=lambda _: None) is False
    assert ledger.events == []


def test_run_resume_claims_the_issue_and_gives_it_back(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _Ledger(monkeypatch)
    _implement_stubs(monkeypatch)
    worktree.create(git_repo, ".worktrees", "issue-131", "fix/issue-131", "main")
    monkeypatch.setattr(
        implement_module, "run_task_loop", lambda *a, **k: LoopOutcome(False, 1, None, [])
    )

    run_resume(git_repo, 131, message="try again", log=lambda _: None)

    assert ledger.events == [("claim", 131), ("release", 131)]


def test_run_resume_does_not_claim_when_there_is_nothing_to_resume(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _Ledger(monkeypatch)

    with pytest.raises(FileNotFoundError):
        run_resume(git_repo, 131, log=lambda _: None)

    assert ledger.events == []


def test_run_spawn_claims_and_leaves_the_release_to_the_session(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`agent spawn` returns while the work is only starting, so releasing here
    would hand the issue back the instant the session got going."""
    from agent_ops.runtimes import claude_code
    from agent_ops.workflows.spawn import run_spawn

    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(claude_code.ClaudeCodeRuntime, "available", lambda self: True)
    monkeypatch.setattr(surfaces, "pick", lambda name="auto": _RecordingSurface())

    run_spawn(git_repo, 131)

    assert ledger.events == [("claim", 131)]


def test_run_spawn_gives_the_claim_back_when_nothing_starts(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed surface attach leaves a worktree and no session. Keeping the
    claim would block every lane on a run that never began."""
    from agent_ops.runtimes import claude_code
    from agent_ops.workflows.spawn import run_spawn

    ledger = _Ledger(monkeypatch)
    monkeypatch.setattr(claude_code.ClaudeCodeRuntime, "available", lambda self: True)
    monkeypatch.setattr(
        surfaces, "pick", lambda name="auto": _RecordingSurface(fail="attach timed out")
    )

    with pytest.raises(CommandError):
        run_spawn(git_repo, 131)

    assert ledger.events == [("claim", 131), ("release", 131)]


class _RecordingSurface:
    name = "fake"

    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail

    def available(self) -> bool:
        return True

    def spawn(
        self, label: str, command: list[str], cwd: Path, attach_path: Path | None = None
    ) -> surfaces.Spawned:
        if self.fail is not None:
            raise CommandError(self.fail)
        return surfaces.Spawned(where="fake surface", surface=self.name, handle="term_worker")


def test_report_outcome_releases_the_claim(git_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """How a spawned session hands the issue back: the session-end hook runs
    `agent report` whether or not the agent remembered to."""
    ledger = _Ledger(monkeypatch)

    report_outcome(git_repo, 131, state="halted", reason="the session ended")

    assert ledger.events == [("release", 131)]


def test_report_outcome_leaves_the_claim_alone_when_already_reported(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--if-unreported` returns before recording anything, and the report that
    did land already released — a second release would strip a claim belonging
    to whatever cycle started next."""
    ledger = _Ledger(monkeypatch)
    runs.write_outcome(git_repo, 131, state="done")

    report_outcome(git_repo, 131, state="halted", if_unreported=True)

    assert ledger.events == []
