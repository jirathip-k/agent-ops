import json
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_ops import registry, status, utils
from agent_ops.claims import CLAIM_LABEL
from agent_ops.cli import app
from agent_ops.registry import RegistryConfig
from agent_ops.status import (
    BUCKETS,
    GATE_STAGES,
    LaneInfo,
    StageEntry,
    _branch,
    _cell,
    _short_runner,
    _tag,
    bucket_counts,
    detect_lanes,
    stage_counts,
)
from agent_ops.utils import CommandError
from agent_ops.workflows.triage import BUCKET_LABELS, GATE_LABELS, TRIAGE_DONE_LABEL

runner = CliRunner()


def _issue(*labels: str) -> dict:
    return {"labels": [{"name": name} for name in labels]}


def test_bucket_counts() -> None:
    issues = [
        _issue("agent-ready"),
        _issue("agent-ready", "type: bug"),
        _issue("needs-human"),
        _issue("backlog"),
        _issue("type: idea"),
        _issue(),
    ]
    assert bucket_counts(issues) == {
        "agent-ready": 2,
        "needs-human": 1,
        "backlog": 1,
        "untriaged": 2,
    }


def test_bucket_counts_empty() -> None:
    assert bucket_counts([]) == {
        "agent-ready": 0,
        "needs-human": 0,
        "backlog": 0,
        "untriaged": 0,
    }


# --- stage_counts / pipeline stage list --------------------------------------


def test_stage_precedence_stays_in_sync_with_the_lanes_own_label_lists() -> None:
    # Drift guard (#150 shape): the pipeline stage list must be derived from
    # the same constants the lanes read and write, not a second hand-typed list.
    assert set(BUCKETS) == BUCKET_LABELS
    assert set(GATE_STAGES) == set(GATE_LABELS)
    assert CLAIM_LABEL in status.STAGE_PRECEDENCE
    assert TRIAGE_DONE_LABEL in status.STAGE_PRECEDENCE


def _pipeline_issue(number: int, created: str, *labels: str) -> dict[str, Any]:
    return {"number": number, "createdAt": created, "labels": [{"name": name} for name in labels]}


def test_stage_counts_empty() -> None:
    counts = stage_counts([])
    assert all(entry == StageEntry(0, None, None) for entry in counts.values())
    assert set(counts) == {*status.STAGE_PRECEDENCE, "untriaged"}


def test_stage_counts_multi_label_issue_counts_once_at_the_furthest_stage() -> None:
    issues = [
        # agent-ready + claimed: in-flight work, not two items.
        _pipeline_issue(1, "2026-07-20T00:00:00Z", "agent-ready", CLAIM_LABEL),
        # backlog + spec-requested: further along than plain backlog.
        _pipeline_issue(2, "2026-07-21T00:00:00Z", "backlog", "spec-requested"),
        # agent-ready + plan-requested: further along than plain agent-ready.
        _pipeline_issue(3, "2026-07-22T00:00:00Z", "agent-ready", "plan-requested"),
        _pipeline_issue(4, "2026-07-23T00:00:00Z", TRIAGE_DONE_LABEL),
        _pipeline_issue(5, "2026-07-24T00:00:00Z"),  # no relevant label at all
    ]

    counts = stage_counts(issues)

    assert counts[CLAIM_LABEL].count == 1
    assert counts["plan-requested"].count == 1
    assert counts["spec-requested"].count == 1
    assert counts["agent-ready"].count == 0  # both agent-ready issues outrank to a gate/claim
    assert counts["backlog"].count == 0
    assert counts[TRIAGE_DONE_LABEL].count == 1
    assert counts["untriaged"].count == 1
    assert sum(entry.count for entry in counts.values()) == len(issues)


def test_stage_counts_oldest_issue_sets_the_stages_age_and_number() -> None:
    issues = [
        _pipeline_issue(10, "2026-07-22T00:00:00Z", "agent-ready"),
        _pipeline_issue(5, "2026-07-18T00:00:00Z", "agent-ready"),
        _pipeline_issue(9, "2026-07-20T00:00:00Z", "agent-ready"),
    ]

    entry = stage_counts(issues)["agent-ready"]

    assert entry.count == 3
    assert entry.oldest_created_at == "2026-07-18T00:00:00Z"
    assert entry.oldest_number == 5


def test_age_formats_days_hours_minutes_and_under_a_minute() -> None:
    now = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
    assert status._age("2026-07-24T12:00:00Z", now=now) == "4d"
    assert status._age("2026-07-28T09:00:00Z", now=now) == "3h"
    assert status._age("2026-07-28T11:48:00Z", now=now) == "12m"
    assert status._age("2026-07-28T11:59:30Z", now=now) == "<1m"


def _stub(
    lane: str,
    ref: str = "@main",
    owner: str = "jirathip-k",
    runner: str | None = None,
    cron: str | None = None,
) -> str:
    on_block = "on:\n"
    if cron is not None:
        on_block += f'  schedule:\n    - cron: "{cron}"\n'
    on_block += "  workflow_dispatch:\n"
    with_block = "    with:\n      target_repo: my/repo\n"
    if runner is not None:
        with_block += f"      runner: {runner}\n"
    return (
        f"name: {lane}\n"
        f"{on_block}"
        "jobs:\n"
        f"  {lane}:\n"
        f"    uses: {owner}/agent-ops/.github/workflows/{lane}-pipeline.yml{ref}\n"
        f"{with_block}"
    )


def _lane(
    runner: str | None = None, cron: str | None = None, caller_file: str = "x.yml"
) -> LaneInfo:
    return LaneInfo(runner=runner, cron=cron, caller_file=caller_file)


def test_detect_lanes_cross_repo_uses_no_runner() -> None:
    assert detect_lanes({"triage.yml": _stub("triage")}) == {
        "triage": _lane(caller_file="triage.yml")
    }


def test_detect_lanes_runner_passed() -> None:
    stub = _stub("triage", runner="blacksmith-2vcpu-ubuntu-2404")
    assert detect_lanes({"triage.yml": stub}) == {
        "triage": _lane(runner="blacksmith-2vcpu-ubuntu-2404", caller_file="triage.yml")
    }


def test_detect_lanes_cron_and_runner() -> None:
    stub = _stub("triage", runner="blacksmith-2vcpu-ubuntu-2404", cron="17 * * * *")
    assert detect_lanes({"triage.yml": stub}) == {
        "triage": _lane(
            runner="blacksmith-2vcpu-ubuntu-2404", cron="17 * * * *", caller_file="triage.yml"
        )
    }


def test_detect_lanes_dispatch_only_stub_has_no_cron() -> None:
    # _stub emits on: workflow_dispatch: when no cron is given.
    assert detect_lanes({"spec.yml": _stub("spec")}) == {
        "spec": _lane(cron=None, caller_file="spec.yml")
    }


def test_detect_lanes_cron_applies_only_to_lanes_in_that_file() -> None:
    workflows = {
        "triage.yml": _stub("triage", cron="0 * * * *"),
        "spec.yml": _stub("spec"),
    }
    assert detect_lanes(workflows) == {
        "triage": _lane(cron="0 * * * *", caller_file="triage.yml"),
        "spec": _lane(cron=None, caller_file="spec.yml"),
    }


def test_detect_lanes_any_owner() -> None:
    assert detect_lanes({"groom.yml": _stub("groom", owner="someone-else")}) == {
        "groom": _lane(caller_file="groom.yml")
    }


def test_detect_lanes_local_uses() -> None:
    content = "jobs:\n  triage:\n    uses: ./.github/workflows/triage-pipeline.yml\n"
    assert detect_lanes({"self.yml": content}) == {"triage": _lane(caller_file="self.yml")}


def test_detect_lanes_filename_does_not_matter() -> None:
    # Detection is content-based: a renamed stub still counts, and the caller
    # file name it reports is that same renamed stub, not a guessed default.
    assert detect_lanes({"nightly-cleanup.yml": _stub("groom")}) == {
        "groom": _lane(caller_file="nightly-cleanup.yml")
    }


def test_detect_lanes_multiple_lanes_one_file_share_the_cron() -> None:
    content = (
        "name: agents\n"
        "on:\n"
        "  schedule:\n"
        '    - cron: "0 6 * * *"\n'
        "jobs:\n"
        "  triage:\n"
        "    uses: jirathip-k/agent-ops/.github/workflows/triage-pipeline.yml@main\n"
        "    with:\n"
        "      runner: blacksmith-2vcpu-ubuntu-2404\n"
        "  groom:\n"
        "    uses: jirathip-k/agent-ops/.github/workflows/groom-pipeline.yml@main\n"
        "    with:\n"
        "      runner: blacksmith-4vcpu-ubuntu-2404\n"
    )
    assert detect_lanes({"agents.yml": content}) == {
        "triage": _lane(
            runner="blacksmith-2vcpu-ubuntu-2404", cron="0 6 * * *", caller_file="agents.yml"
        ),
        "groom": _lane(
            runner="blacksmith-4vcpu-ubuntu-2404", cron="0 6 * * *", caller_file="agents.yml"
        ),
    }


def test_detect_lanes_spec_plan_and_promote() -> None:
    workflows = {
        "spec.yaml": _stub("spec"),
        "plan.yml": _stub("plan", ref="@v2"),
        "promote.yml": _stub("promote", runner="blacksmith-4vcpu-ubuntu-2404"),
    }
    assert detect_lanes(workflows) == {
        "spec": _lane(caller_file="spec.yaml"),
        "plan": _lane(caller_file="plan.yml"),
        "promote": _lane(runner="blacksmith-4vcpu-ubuntu-2404", caller_file="promote.yml"),
    }


def test_detect_lanes_scout_stub_cron_no_runner() -> None:
    stub = _stub("scout", cron="0 18 * * *")
    assert detect_lanes({"scout.yml": stub}) == {
        "scout": _lane(cron="0 18 * * *", caller_file="scout.yml")
    }


def test_detect_lanes_yaml_extension_in_uses() -> None:
    content = "jobs:\n  t:\n    uses: o/agent-ops/.github/workflows/triage-pipeline.yaml@main\n"
    assert detect_lanes({"t.yml": content}) == {"triage": _lane(caller_file="t.yml")}


def test_detect_lanes_ignores_unrelated_workflows() -> None:
    deploy = (
        "name: deploy\n"
        "jobs:\n"
        "  deploy:\n"
        "    uses: actions/deploy-pages@v4\n"
        "    # mentions triage and groom in a comment only\n"
    )
    assert detect_lanes({"deploy.yml": deploy}) == {}


def test_detect_lanes_unparseable_yaml_falls_back_to_line_scan() -> None:
    content = (
        "jobs: [\n"  # unclosed flow sequence → YAMLError
        "  triage:\n"
        "    uses: jirathip-k/agent-ops/.github/workflows/triage-pipeline.yml@main\n"
    )
    # The fallback line scan still knows which file it read the `uses:` line
    # from, even though it gave up on runner/cron — `caller_file` must not
    # silently drop to some default in the branch that skips YAML parsing.
    assert detect_lanes({"broken.yml": content}) == {"triage": _lane(caller_file="broken.yml")}


def test_detect_lanes_empty() -> None:
    assert detect_lanes({}) == {}


# --- local_deployed_lanes ------------------------------------------------


def test_local_deployed_lanes_no_workflows_dir_is_empty_set(tmp_path: Path) -> None:
    assert status.local_deployed_lanes(tmp_path) == set()


def test_local_deployed_lanes_empty_workflows_dir_is_empty_set(tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    assert status.local_deployed_lanes(tmp_path) == set()


def test_local_deployed_lanes_detects_a_wired_lane(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "triage.yml").write_text(_stub("triage"))
    assert status.local_deployed_lanes(tmp_path) == {"triage"}


def test_local_deployed_lanes_multiple_files_yml_and_yaml(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "triage.yml").write_text(_stub("triage"))
    (workflows / "groom.yaml").write_text(_stub("groom"))
    (workflows / "README.md").write_text("not a workflow")
    assert status.local_deployed_lanes(tmp_path) == {"triage", "groom"}


def test_local_deployed_lanes_unreadable_directory_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    original_iterdir = Path.iterdir

    def fake_iterdir(self: Path) -> Any:
        if self == workflows:
            raise OSError("permission denied")
        return original_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", fake_iterdir)
    assert status.local_deployed_lanes(tmp_path) is None


def test_local_deployed_lanes_unreadable_file_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    bad = workflows / "triage.yml"
    bad.write_text(_stub("triage"))
    original_read_text = Path.read_text

    def fake_read_text(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == bad:
            raise OSError("permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)
    assert status.local_deployed_lanes(tmp_path) is None


def test_short_runner_labels() -> None:
    assert _short_runner(None) == "gh"
    assert _short_runner("blacksmith-2vcpu-ubuntu-2404") == "bs-2vcpu"
    assert _short_runner("blacksmith-4vcpu-ubuntu-2404") == "bs-4vcpu"
    assert _short_runner("blacksmith-8vcpu-macos-15") == "bs-8vcpu-mac"
    assert _short_runner("self-hosted") == "self-hosted"  # unknown labels pass through


def test_cell_appends_star_for_cron_scheduled_lanes() -> None:
    assert _cell(_lane(runner="blacksmith-2vcpu-ubuntu-2404", cron="17 * * * *")) == "bs-2vcpu*"
    assert _cell(_lane(cron="0 6 * * *")) == "gh*"
    assert _cell(_lane(runner="blacksmith-2vcpu-ubuntu-2404")) == "bs-2vcpu"
    assert _cell(_lane()) == "gh"


# --- fetch_repos --------------------------------------------------------------
#
# The shared concurrency primitive behind `fleet_status`, `pipeline_status`
# and the TUI's fleet load (#232's post-review fix: a serial sweep measured at
# 47s against seven repos).


def test_fetch_repos_runs_concurrently_not_one_repo_at_a_time() -> None:
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fetch(repo: str) -> str:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return repo

    results = status.fetch_repos(["o/a", "o/b", "o/c"], fetch)

    # A serial loop could never have more than one fetch in flight at once.
    assert max_active > 1
    assert results == [("o/a", "o/a"), ("o/b", "o/b"), ("o/c", "o/c")]


def test_fetch_repos_bounds_concurrency_to_max_workers() -> None:
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fetch(repo: str) -> None:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1

    status.fetch_repos([f"o/{i}" for i in range(10)], fetch, max_workers=2)

    # A 30-repo fleet must not fork 30 concurrent `gh` processes.
    assert max_active <= 2


def test_fetch_repos_returns_in_repos_order_regardless_of_completion_order() -> None:
    # o/a is deliberately the slowest — the returned order must still follow
    # `repos`, not whichever repo's fetch happened to finish first.
    def fetch(repo: str) -> str:
        if repo == "o/a":
            time.sleep(0.05)
        return repo

    results = status.fetch_repos(["o/a", "o/b", "o/c"], fetch)

    assert [repo for repo, _ in results] == ["o/a", "o/b", "o/c"]


def test_fetch_repos_isolates_one_repos_failure_from_the_rest() -> None:
    def fetch(repo: str) -> str:
        if repo == "o/bad":
            raise CommandError("boom")
        return repo

    results = dict(status.fetch_repos(["o/good1", "o/bad", "o/good2"], fetch))

    assert results["o/good1"] == "o/good1"
    assert results["o/good2"] == "o/good2"
    assert isinstance(results["o/bad"], CommandError)
    assert str(results["o/bad"]) == "boom"


def test_fetch_repos_on_result_fires_once_per_repo() -> None:
    seen: dict[str, str | CommandError] = {}

    def record(repo: str, result: str | CommandError) -> None:
        seen[repo] = result

    status.fetch_repos(["o/a", "o/b"], lambda repo: repo.upper(), on_result=record)

    assert seen == {"o/a": "O/A", "o/b": "O/B"}


def test_fetch_repos_empty_repos_returns_empty_without_calling_fetch() -> None:
    def unreachable(repo: str) -> str:
        raise AssertionError("fetch must not be called for an empty repo list")

    assert status.fetch_repos([], unreachable) == []


# --- fleet_failures ----------------------------------------------------------


Proc = subprocess.CompletedProcess[str]
FakeRun = Callable[..., Proc]


def _proc(stdout: str = "", *, returncode: int = 0, stderr: str = "") -> Proc:
    return subprocess.CompletedProcess(["gh"], returncode, stdout=stdout, stderr=stderr)


def _run(
    name: str,
    branch: str = "main",
    created: str = "2026-07-26T08:14:22Z",
    conclusion: str = "failure",
) -> dict[str, Any]:
    return {
        "name": name,
        "conclusion": conclusion,
        "headBranch": branch,
        "createdAt": created,
        "url": f"https://github.com/o/r/actions/runs/{len(name)}",
    }


def _canned(responses: dict[str, Proc]) -> FakeRun:
    """A `run()` stand-in that answers per the `--repo` argument it is given.

    The sweep asks once per failed conclusion, so each repo's canned payload is
    narrowed to the `--status` being asked for — the same filtering `gh` does
    server-side.
    """

    def fake(cmd: list[str], **kwargs: Any) -> Proc:
        proc = responses[cmd[cmd.index("--repo") + 1]]
        if proc.returncode != 0:
            return proc
        wanted = cmd[cmd.index("--status") + 1]
        runs = [r for r in json.loads(proc.stdout) if r.get("conclusion") == wanted]
        return _proc(json.dumps(runs))

    return fake


def test_fleet_failures_groups_results_per_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _canned(
            {
                "o/a": _proc(json.dumps([_run("triage", "main"), _run("ci", "feat/x")])),
                "o/b": _proc(json.dumps([_run("deploy", "staging")])),
            }
        ),
    )
    lines: list[str] = []

    status.fleet_failures(RegistryConfig(repos=["o/a", "o/b"]), log=lines.append)

    text = "\n".join(lines)
    assert "o/a\033[0m — 2 recent failed run(s)" in text
    assert "o/b\033[0m — 1 recent failed run(s)" in text
    # Each failure is its own line under its repo's header, newest-first order kept.
    a_section = text.split("o/a")[1].split("o/b")[0]
    assert "triage" in a_section and "ci" in a_section and "deploy" not in a_section
    assert "07-26 08:14" in a_section and "feat/x" in a_section
    assert "2 repo(s) with failures · 0 clean" in text


def test_fleet_failures_empty_registry_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(cmd: list[str], **kwargs: Any) -> Proc:
        raise AssertionError("no repos registered — gh must not be called")

    monkeypatch.setattr(status, "run", unreachable)
    lines: list[str] = []

    status.fleet_failures(RegistryConfig(repos=[]), log=lines.append)

    assert len(lines) == 1
    assert "no repos registered" in lines[0]


def test_fleet_failures_zero_failures_is_one_clean_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        status, "run", _canned({"o/a": _proc("[]"), "o/b": _proc("[]"), "o/c": _proc("[]")})
    )
    lines: list[str] = []

    status.fleet_failures(RegistryConfig(repos=["o/a", "o/b", "o/c"]), log=lines.append)

    # A clean fleet stays short: no per-repo section, one summary line.
    assert lines == ["✓ 3 repo(s) clean — no recent failed runs"]


def test_fleet_failures_skips_a_repo_that_404s_and_finishes_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _canned(
            {
                # Verbatim stderr from a real `gh run list` on a repo that
                # does not exist / isn't visible to the operator's auth.
                "o/gone": _proc(
                    returncode=1,
                    stderr=(
                        "failed to get runs: HTTP 404: Not Found "
                        "(https://api.github.com/repos/o/gone/actions/runs?status=failure)\n"
                    ),
                ),
                "o/b": _proc(json.dumps([_run("triage")])),
                "o/c": _proc("[]"),
            }
        ),
    )
    lines: list[str] = []

    status.fleet_failures(RegistryConfig(repos=["o/gone", "o/b", "o/c"]), log=lines.append)

    text = "\n".join(lines)
    assert "o/gone\033[0m — skipped: failed to get runs: HTTP 404: Not Found" in text
    # The sweep continued past the unreadable repo.
    assert "o/b\033[0m — 1 recent failed run(s)" in text
    assert "1 repo(s) with failures · 1 clean · 1 skipped (o/gone)" in text


def test_fleet_failures_bounds_each_repo_below_the_run_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    def fake(cmd: list[str], **kwargs: Any) -> Proc:
        seen.append(kwargs.get("timeout"))
        return _proc("[]")

    monkeypatch.setattr(status, "run", fake)

    status.fleet_failures(RegistryConfig(repos=["o/a"]), log=lambda line: None)

    # A fan-out must not let one wedged repo hold the sweep for the fleet-wide default.
    # Every per-conclusion ask carries the bound; none falls back to the default.
    assert seen == [status.FAILURE_TIMEOUT_S] * len(status.FAILED_CONCLUSIONS)
    assert status.FAILURE_TIMEOUT_S < utils.DEFAULT_TIMEOUT_S


def test_fleet_failures_columns_stay_aligned_within_a_repo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _canned(
            {
                "o/a": _proc(
                    json.dumps(
                        [
                            _run("CI", "main"),
                            _run("Deploy Edge Functions", "fix/issue-31"),
                        ]
                    )
                )
            }
        ),
    )
    lines: list[str] = []

    status.fleet_failures(RegistryConfig(repos=["o/a"]), log=lines.append)

    urls = [line.index("https://") for line in lines if "https://" in line]
    assert len(urls) == 2 and len(set(urls)) == 1  # URLs start in the same column


def test_fleet_failures_asks_gh_for_every_failed_conclusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []

    def fake(cmd: list[str], **kwargs: Any) -> Proc:
        asked.append(cmd[cmd.index("--status") + 1])
        return _proc("[]")

    monkeypatch.setattr(status, "run", fake)

    status.fleet_failures(RegistryConfig(repos=["o/a"]), log=lambda line: None)

    assert asked == ["failure", "startup_failure", "cancelled", "timed_out"]


def test_fleet_failures_counts_every_failed_conclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _canned(
            {
                "o/a": _proc(
                    json.dumps(
                        [
                            _run("ci", created="2026-07-25T01:00:00Z"),
                            _run("triage", created="2026-07-25T02:00:00Z", conclusion="cancelled"),
                            _run("groom", created="2026-07-25T03:00:00Z", conclusion="timed_out"),
                            _run(
                                "promote",
                                created="2026-07-25T04:00:00Z",
                                conclusion="startup_failure",
                            ),
                        ]
                    )
                )
            }
        ),
    )
    lines: list[str] = []

    status.fleet_failures(RegistryConfig(repos=["o/a"]), log=lines.append)

    text = "\n".join(lines)
    # All four count as failures — none of the three new ones is invisible.
    assert "o/a\033[0m — 4 recent failed run(s)" in text
    assert "1 repo(s) with failures · 0 clean" in text
    for tag in ("failed", "cancelled", "timed-out", "startup"):
        assert tag in text
    # Merged newest-first across the four separate `gh` asks.
    order = [line.split()[0] for line in lines if line.startswith("  ")]
    assert order == ["promote", "groom", "triage", "ci"]


def test_fleet_failures_catches_a_lane_dead_with_only_startup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The outage that motivated this: five straight startup_failure runs and not
    # one plain `failure`, which the old sweep reported as clean.
    dead = [
        _run("triage", created=f"2026-07-2{day}T06:00:00Z", conclusion="startup_failure")
        for day in range(1, 6)
    ]
    monkeypatch.setattr(
        status, "run", _canned({"o/dead": _proc(json.dumps(dead)), "o/ok": _proc("[]")})
    )
    lines: list[str] = []

    status.fleet_failures(RegistryConfig(repos=["o/dead", "o/ok"]), log=lines.append)

    text = "\n".join(lines)
    assert "o/dead\033[0m — 5 recent failed run(s) · ⚠ 5 startup_failure" in text
    assert "1 repo(s) with failures · 1 clean" in text
    # startup_failure is contract drift, so the report points at the tool that finds it.
    assert "agent doctor" in text and "o/dead" in text.split("agent doctor")[1]


def test_fleet_failures_omits_the_doctor_hint_without_startup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _canned({"o/a": _proc(json.dumps([_run("ci"), _run("deploy", conclusion="timed_out")]))}),
    )
    lines: list[str] = []

    status.fleet_failures(RegistryConfig(repos=["o/a"]), log=lines.append)

    text = "\n".join(lines)
    assert "agent doctor" not in text and "startup_failure" not in text


def test_fleet_failures_keeps_only_the_newest_across_conclusions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs = [
        _run("old", created="2026-07-20T00:00:00Z"),
        _run("mid", created="2026-07-22T00:00:00Z", conclusion="cancelled"),
        _run("new", created="2026-07-24T00:00:00Z", conclusion="startup_failure"),
    ]
    monkeypatch.setattr(status, "run", _canned({"o/a": _proc(json.dumps(runs))}))
    lines: list[str] = []

    status.fleet_failures(RegistryConfig(repos=["o/a"]), log=lines.append, limit=2)

    text = "\n".join(lines)
    # The limit applies to the merged result, not to each conclusion's ask.
    assert "o/a\033[0m — 2 recent failed run(s)" in text
    assert "new" in text and "mid" in text and "old" not in text


def test_fleet_failures_columns_stay_aligned_across_mixed_conclusions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _canned(
            {
                "o/a": _proc(
                    json.dumps(
                        [
                            _run("CI", "main"),
                            _run("Deploy Edge Functions", "fix/issue-31", conclusion="cancelled"),
                            _run("triage", "main", conclusion="startup_failure"),
                        ]
                    )
                )
            }
        ),
    )
    lines: list[str] = []

    status.fleet_failures(RegistryConfig(repos=["o/a"]), log=lines.append)

    # The widest tag ("cancelled") must not stagger the columns after it.
    urls = [line.index("https://") for line in lines if "https://" in line]
    assert len(urls) == 3 and len(set(urls)) == 1


def test_tag_labels_each_conclusion_and_passes_unknown_through() -> None:
    assert _tag({"conclusion": "failure"}) == "failed"
    assert _tag({"conclusion": "startup_failure"}) == "startup"
    assert _tag({"conclusion": "cancelled"}) == "cancelled"
    assert _tag({"conclusion": "timed_out"}) == "timed-out"
    # An unmapped conclusion must not be flattened into a misleading "failed".
    assert _tag({"conclusion": "stale"}) == "stale"
    assert _tag({}) == "?"


def test_branch_elides_only_when_it_would_push_the_url_out() -> None:
    assert _branch({"headBranch": "main"}) == "main"
    short = "a" * status.BRANCH_WIDTH
    assert _branch({"headBranch": short}) == short
    long = "jirathip-k/ci-linux-swift-tests-and-more"
    elided = _branch({"headBranch": long})
    assert len(elided) == status.BRANCH_WIDTH
    # Both ends survive: the lane prefix and the issue suffix are what identify it.
    assert elided.startswith("jirathip-k/") and elided.endswith("more")


def test_open_prs_requests_closing_issues_references(monkeypatch: pytest.MonkeyPatch) -> None:
    """The TUI's issue detail pane (#235) derives PR status from this same
    listing via `github.pr_references_issue`, which needs this field — a
    second `gh pr list` call shape would be the #150 gap this module's
    docstring rules out."""
    captured: dict[str, list[str]] = {}

    def fake(cmd: list[str], **kwargs: Any) -> Proc:
        captured["cmd"] = cmd
        return _proc("[]")

    monkeypatch.setattr(status, "run", fake)

    status._open_prs("o/a")

    json_index = captured["cmd"].index("--json")
    fields = captured["cmd"][json_index + 1].split(",")
    assert "closingIssuesReferences" in fields


# --- fleet_status --------------------------------------------------------------


def _fleet_run(
    prs_by_repo: dict[str, list[dict[str, Any]]],
    issues_by_repo: dict[str, list[dict[str, Any]] | CommandError],
) -> FakeRun:
    """A `run()` stand-in for the two calls `fleet_status` makes per repo:
    `gh pr list` and `gh issue list`, dispatched on `cmd[1]`."""

    def fake(cmd: list[str], **kwargs: Any) -> Proc:
        repo = cmd[cmd.index("--repo") + 1]
        if cmd[1] == "pr":
            return _proc(json.dumps(prs_by_repo.get(repo, [])))
        payload = issues_by_repo[repo]
        if isinstance(payload, CommandError):
            raise payload
        return _proc(json.dumps(payload))

    return fake


def test_fleet_status_lists_prs_and_issue_buckets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _fleet_run(
            prs_by_repo={
                "o/a": [{"number": 3, "title": "x", "baseRefName": "main", "headRefName": "fix/x"}]
            },
            issues_by_repo={"o/a": [_issue("agent-ready")]},
        ),
    )
    lines: list[str] = []

    status.fleet_status(RegistryConfig(repos=["o/a"]), log=lines.append)

    text = "\n".join(lines)
    assert "o/a\033[0m — 1 open issue(s): 1 agent-ready" in text
    assert "PR #3 → main" in text


def test_fleet_status_skips_a_repo_that_404s_and_finishes_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _fleet_run(
            prs_by_repo={"o/b": []},
            issues_by_repo={
                "o/gone": CommandError("HTTP 404: Not Found"),
                "o/b": [],
            },
        ),
    )
    lines: list[str] = []

    status.fleet_status(RegistryConfig(repos=["o/gone", "o/b"]), log=lines.append)

    text = "\n".join(lines)
    assert "o/gone\033[0m — skipped: HTTP 404: Not Found" in text
    # The sweep continued past the unreadable repo.
    assert "o/b\033[0m — 0 open issue(s)" in text


def test_fleet_status_output_order_matches_registry_order(monkeypatch: pytest.MonkeyPatch) -> None:
    # o/a is deliberately the slow one; the printed order must still follow
    # the registry, not whichever repo's `gh` calls happened to return first.
    def fake(cmd: list[str], **kwargs: Any) -> Proc:
        if cmd[cmd.index("--repo") + 1] == "o/a":
            time.sleep(0.05)
        return _proc("[]")

    monkeypatch.setattr(status, "run", fake)
    lines: list[str] = []

    status.fleet_status(RegistryConfig(repos=["o/a", "o/b", "o/c"]), log=lines.append)

    text = "\n".join(lines)
    assert text.index("o/a\033[0m") < text.index("o/b\033[0m") < text.index("o/c\033[0m")


# --- pipeline_status -----------------------------------------------------


def _issues_by_repo(mapping: dict[str, Proc]) -> FakeRun:
    def fake(cmd: list[str], **kwargs: Any) -> Proc:
        return mapping[cmd[cmd.index("--repo") + 1]]

    return fake


def test_pipeline_status_empty_registry_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    def unreachable(cmd: list[str], **kwargs: Any) -> Proc:
        raise AssertionError("no repos registered — gh must not be called")

    monkeypatch.setattr(status, "run", unreachable)
    lines: list[str] = []

    status.pipeline_status(RegistryConfig(repos=[]), log=lines.append)

    assert len(lines) == 1
    assert "no repos registered" in lines[0]


def test_pipeline_status_empty_repo_says_no_open_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(status, "run", _issues_by_repo({"o/a": _proc("[]")}))
    lines: list[str] = []

    status.pipeline_status(RegistryConfig(repos=["o/a"]), log=lines.append)

    text = "\n".join(lines)
    assert "o/a\033[0m — 0 open issue(s)" in text
    assert "no open issues" in text


def test_pipeline_status_multi_label_issue_counts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    issues = [
        _pipeline_issue(1, "2026-07-20T00:00:00Z", "agent-ready", CLAIM_LABEL),
        _pipeline_issue(2, "2026-07-21T00:00:00Z", "backlog"),
    ]
    monkeypatch.setattr(status, "run", _issues_by_repo({"o/a": _proc(json.dumps(issues))}))
    monkeypatch.setattr(status, "_deployed_lanes", lambda repo: {"groom"})
    lines: list[str] = []

    status.pipeline_status(RegistryConfig(repos=["o/a"]), log=lines.append)

    stage_lines = {line.split()[0]: line for line in lines if line.startswith("  ")}
    assert stage_lines[CLAIM_LABEL].split()[1] == "1"
    assert stage_lines["backlog"].split()[1] == "1"
    assert "agent-ready" not in stage_lines  # outranked by the claim label, not double-counted


def test_pipeline_status_skips_a_repo_that_404s_and_finishes_the_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _issues_by_repo(
            {
                "o/gone": _proc(
                    returncode=1,
                    stderr="failed to list issues: HTTP 404: Not Found (...)\n",
                ),
                "o/b": _proc(
                    json.dumps([_pipeline_issue(1, "2026-07-20T00:00:00Z", "agent-ready")])
                ),
            }
        ),
    )
    monkeypatch.setattr(status, "_deployed_lanes", lambda repo: {"triage"})
    lines: list[str] = []

    status.pipeline_status(RegistryConfig(repos=["o/gone", "o/b"]), log=lines.append)

    text = "\n".join(lines)
    assert "o/gone\033[0m — skipped: failed to list issues: HTTP 404: Not Found" in text
    # The sweep continued past the unreadable repo.
    assert "o/b\033[0m — 1 open issue(s)" in text
    assert "1 repo(s) skipped (o/gone)" in text


def test_pipeline_status_flags_a_truncated_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    limit = status.PIPELINE_ISSUE_LIMIT
    issues = [_pipeline_issue(i, "2026-07-20T00:00:00Z", "agent-ready") for i in range(limit)]
    monkeypatch.setattr(status, "run", _issues_by_repo({"o/a": _proc(json.dumps(issues))}))
    monkeypatch.setattr(status, "_deployed_lanes", lambda repo: {"triage"})
    lines: list[str] = []

    status.pipeline_status(RegistryConfig(repos=["o/a"]), log=lines.append)

    text = "\n".join(lines)
    assert "listing truncated" in text
    assert f"≥{limit}" in text


def test_pipeline_status_below_the_limit_has_no_truncation_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issues = [_pipeline_issue(1, "2026-07-20T00:00:00Z", "agent-ready")]
    monkeypatch.setattr(status, "run", _issues_by_repo({"o/a": _proc(json.dumps(issues))}))
    monkeypatch.setattr(status, "_deployed_lanes", lambda repo: {"triage"})
    lines: list[str] = []

    status.pipeline_status(RegistryConfig(repos=["o/a"]), log=lines.append)

    text = "\n".join(lines)
    assert "truncated" not in text
    assert "≥" not in text


# --- pipeline_status: unserviced-stage detection (#229) ----------------------


def test_pipeline_status_flags_stage_with_no_deployed_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    # The dotfiles#1 case the issue was filed about: a repo with no lanes
    # deployed at all. Assert on the stage row itself, not the full output —
    # the trailing legend line also contains the words "⚠ unserviced", so a
    # marker that never fired on the row would still pass a whole-text check.
    issues = [_pipeline_issue(1, "2026-07-20T00:00:00Z", "agent-ready")]
    monkeypatch.setattr(status, "run", _issues_by_repo({"o/a": _proc(json.dumps(issues))}))
    monkeypatch.setattr(status, "_deployed_lanes", lambda repo: set())
    lines: list[str] = []

    status.pipeline_status(RegistryConfig(repos=["o/a"]), log=lines.append)

    stage_line = next(line for line in lines if line.strip().startswith("agent-ready"))
    assert "⚠ unserviced" in stage_line


def test_pipeline_status_partial_lanes_flags_only_unconsumed_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issues = [
        _pipeline_issue(1, "2026-07-20T00:00:00Z", "agent-ready"),
        _pipeline_issue(2, "2026-07-21T00:00:00Z"),  # untriaged
        _pipeline_issue(3, "2026-07-22T00:00:00Z", "backlog"),
    ]
    monkeypatch.setattr(status, "run", _issues_by_repo({"o/a": _proc(json.dumps(issues))}))
    # triage services both untriaged and agent-ready here; groom is missing,
    # so backlog — serviced only by groom — must be the one flagged.
    monkeypatch.setattr(status, "_deployed_lanes", lambda repo: {"triage"})
    lines: list[str] = []

    status.pipeline_status(RegistryConfig(repos=["o/a"]), log=lines.append)

    stage_lines = {
        line.split()[0]: line
        for line in lines
        if line.startswith("  ") and line.split()[0] in ("agent-ready", "untriaged", "backlog")
    }
    assert "⚠ unserviced" not in stage_lines["agent-ready"]
    assert "⚠ unserviced" not in stage_lines["untriaged"]
    assert "⚠ unserviced" in stage_lines["backlog"]


def test_pipeline_status_spec_requested_unserviced_without_spec_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issues = [_pipeline_issue(1, "2026-07-20T00:00:00Z", "spec-requested")]
    monkeypatch.setattr(status, "run", _issues_by_repo({"o/a": _proc(json.dumps(issues))}))
    monkeypatch.setattr(status, "_deployed_lanes", lambda repo: {"triage"})
    lines: list[str] = []

    status.pipeline_status(RegistryConfig(repos=["o/a"]), log=lines.append)

    text = "\n".join(lines)
    spec_line = next(line for line in lines if line.strip().startswith("spec-requested"))
    assert "⚠ unserviced" in spec_line
    assert "unserviced check skipped" not in text


def test_pipeline_status_fully_wired_repo_has_no_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    issues = [
        _pipeline_issue(1, "2026-07-20T00:00:00Z", "agent-ready"),
        _pipeline_issue(2, "2026-07-21T00:00:00Z", "backlog"),
        _pipeline_issue(3, "2026-07-22T00:00:00Z", "spec-requested"),
        _pipeline_issue(4, "2026-07-23T00:00:00Z", "plan-requested"),
        _pipeline_issue(5, "2026-07-24T00:00:00Z"),  # untriaged
    ]
    monkeypatch.setattr(status, "run", _issues_by_repo({"o/a": _proc(json.dumps(issues))}))
    monkeypatch.setattr(status, "_deployed_lanes", lambda repo: {"triage", "groom", "spec", "plan"})
    lines: list[str] = []

    status.pipeline_status(RegistryConfig(repos=["o/a"]), log=lines.append)

    text = "\n".join(lines)
    assert "⚠ unserviced —" not in text


def test_pipeline_status_needs_human_never_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    issues = [_pipeline_issue(1, "2026-07-20T00:00:00Z", "needs-human")]
    monkeypatch.setattr(status, "run", _issues_by_repo({"o/a": _proc(json.dumps(issues))}))

    def unreachable(repo: str) -> set[str] | None:
        raise AssertionError("needs-human alone must not trigger the lane-wiring API call")

    monkeypatch.setattr(status, "_deployed_lanes", unreachable)
    lines: list[str] = []

    status.pipeline_status(RegistryConfig(repos=["o/a"]), log=lines.append)

    text = "\n".join(lines)
    assert "needs-human" in text
    assert "⚠ unserviced —" not in text


def test_pipeline_status_unreadable_lane_wiring_reports_uncertainty_not_unserviced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issues = [_pipeline_issue(1, "2026-07-20T00:00:00Z", "agent-ready")]
    monkeypatch.setattr(status, "run", _issues_by_repo({"o/a": _proc(json.dumps(issues))}))
    monkeypatch.setattr(status, "_deployed_lanes", lambda repo: None)
    lines: list[str] = []

    status.pipeline_status(RegistryConfig(repos=["o/a"]), log=lines.append)

    text = "\n".join(lines)
    assert "unserviced check skipped" in text
    assert "⚠ unserviced —" not in text


def test_stage_consumers_values_are_real_lanes_and_gate_stages_are_derived() -> None:
    # Drift guard (#150 shape): every consumer named here must be a lane that
    # actually exists, and the gate-stage entries must come from GATE_STAGES
    # itself rather than a second hand-typed copy.
    all_lanes = set(status.LANES)
    for stage, consumers in status.STAGE_CONSUMERS.items():
        assert consumers <= all_lanes, f"{stage} names a lane that doesn't exist: {consumers}"
    for stage in GATE_STAGES:
        assert status.STAGE_CONSUMERS[stage] == {stage.removesuffix("-requested")}
    assert "needs-human" not in status.STAGE_CONSUMERS


# --- own_repo_startup_failures ------------------------------------------------


def _api_run(fixtures: dict[str, Proc]) -> FakeRun:
    """A `run()` stand-in for the two `gh api` calls the check makes,
    dispatched on which REST path the command hits."""

    def fake(cmd: list[str], **kwargs: Any) -> Proc:
        path = cmd[2]
        if path.endswith("/actions/runs"):
            return fixtures["runs"]
        if path.endswith("/actions/workflows"):
            return fixtures["workflows"]
        raise AssertionError(f"unexpected gh api path: {path}")

    return fake


def _workflow_run(path: str, conclusion: str = "startup_failure") -> dict[str, Any]:
    return {"path": path, "conclusion": conclusion, "name": path}


def _workflow(path: str, state: str = "active") -> dict[str, Any]:
    return {"path": path, "state": state}


def test_own_repo_startup_failures_reports_an_active_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _api_run(
            {
                "runs": _proc(json.dumps([_workflow_run(".github/workflows/evolve.yml")])),
                "workflows": _proc(json.dumps([_workflow(".github/workflows/evolve.yml")])),
            }
        ),
    )

    result = status.own_repo_startup_failures("o/a")

    assert [r["path"] for r in result] == [".github/workflows/evolve.yml"]


def test_own_repo_startup_failures_excludes_a_deliberately_disabled_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _api_run(
            {
                "runs": _proc(
                    json.dumps(
                        [
                            _workflow_run(".github/workflows/evolve.yml"),
                            _workflow_run(".github/workflows/old-lane.yml"),
                        ]
                    )
                ),
                "workflows": _proc(
                    json.dumps(
                        [
                            _workflow(".github/workflows/evolve.yml", state="active"),
                            _workflow(".github/workflows/old-lane.yml", state="disabled_manually"),
                        ]
                    )
                ),
            }
        ),
    )

    result = status.own_repo_startup_failures("o/a")

    # Only the still-enabled workflow is reported — the disabled one failing
    # is expected, and reporting it would just teach a human to ignore this.
    assert [r["path"] for r in result] == [".github/workflows/evolve.yml"]


def test_own_repo_startup_failures_empty_when_no_startup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _api_run({"runs": _proc("[]"), "workflows": _proc(json.dumps([_workflow("x.yml")]))}),
    )

    assert status.own_repo_startup_failures("o/a") == []


def test_own_repo_startup_failures_degrades_to_reporting_everything_when_workflow_list_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `gh api .../actions/workflows` failure must not silently hide a real
    startup failure behind a swallowed exception — it degrades to treating
    every workflow as active rather than suppressing the alert."""
    monkeypatch.setattr(
        status,
        "run",
        _api_run(
            {
                "runs": _proc(json.dumps([_workflow_run(".github/workflows/evolve.yml")])),
                "workflows": _proc(returncode=1, stderr="HTTP 403: no scope"),
            }
        ),
    )

    result = status.own_repo_startup_failures("o/a")

    assert [r["path"] for r in result] == [".github/workflows/evolve.yml"]


def test_own_repo_startup_failures_empty_when_the_runs_call_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status,
        "run",
        _api_run(
            {"runs": _proc(returncode=1, stderr="HTTP 404: Not Found"), "workflows": _proc("[]")}
        ),
    )

    assert status.own_repo_startup_failures("o/a") == []


# --- CLI wiring --------------------------------------------------------------


def test_status_failures_flag_runs_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "load_registry", lambda: RegistryConfig(repos=[]))

    result = runner.invoke(app, ["status", "--failures"])

    assert result.exit_code == 0
    assert "no repos registered" in result.output


def test_status_rejects_failures_with_pipelines() -> None:
    result = runner.invoke(app, ["status", "--failures", "--pipelines"])

    assert result.exit_code == 1
    assert "one at a time" in result.stderr


def test_status_pipeline_flag_runs_the_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "load_registry", lambda: RegistryConfig(repos=[]))

    result = runner.invoke(app, ["status", "--pipeline"])

    assert result.exit_code == 0
    assert "no repos registered" in result.output


def test_status_rejects_pipeline_with_pipelines() -> None:
    result = runner.invoke(app, ["status", "--pipeline", "--pipelines"])

    assert result.exit_code == 1
    assert "one at a time" in result.stderr
