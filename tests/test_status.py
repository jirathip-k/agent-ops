import json
import subprocess
from collections.abc import Callable
from typing import Any

import pytest
from typer.testing import CliRunner

from agent_ops import registry, status, utils
from agent_ops.cli import app
from agent_ops.registry import RegistryConfig
from agent_ops.status import (
    LaneInfo,
    _branch,
    _cell,
    _short_runner,
    bucket_counts,
    detect_lanes,
)

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


def _lane(runner: str | None = None, cron: str | None = None) -> LaneInfo:
    return LaneInfo(runner=runner, cron=cron)


def test_detect_lanes_cross_repo_uses_no_runner() -> None:
    assert detect_lanes({"triage.yml": _stub("triage")}) == {"triage": _lane()}


def test_detect_lanes_runner_passed() -> None:
    stub = _stub("triage", runner="blacksmith-2vcpu-ubuntu-2404")
    assert detect_lanes({"triage.yml": stub}) == {
        "triage": _lane(runner="blacksmith-2vcpu-ubuntu-2404")
    }


def test_detect_lanes_cron_and_runner() -> None:
    stub = _stub("triage", runner="blacksmith-2vcpu-ubuntu-2404", cron="17 * * * *")
    assert detect_lanes({"triage.yml": stub}) == {
        "triage": _lane(runner="blacksmith-2vcpu-ubuntu-2404", cron="17 * * * *")
    }


def test_detect_lanes_dispatch_only_stub_has_no_cron() -> None:
    # _stub emits on: workflow_dispatch: when no cron is given.
    assert detect_lanes({"spec.yml": _stub("spec")}) == {"spec": _lane(cron=None)}


def test_detect_lanes_cron_applies_only_to_lanes_in_that_file() -> None:
    workflows = {
        "triage.yml": _stub("triage", cron="0 * * * *"),
        "spec.yml": _stub("spec"),
    }
    assert detect_lanes(workflows) == {
        "triage": _lane(cron="0 * * * *"),
        "spec": _lane(cron=None),
    }


def test_detect_lanes_any_owner() -> None:
    assert detect_lanes({"groom.yml": _stub("groom", owner="someone-else")}) == {"groom": _lane()}


def test_detect_lanes_local_uses() -> None:
    content = "jobs:\n  triage:\n    uses: ./.github/workflows/triage-pipeline.yml\n"
    assert detect_lanes({"self.yml": content}) == {"triage": _lane()}


def test_detect_lanes_filename_does_not_matter() -> None:
    # Detection is content-based: a renamed stub still counts.
    assert detect_lanes({"nightly-cleanup.yml": _stub("groom")}) == {"groom": _lane()}


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
        "triage": _lane(runner="blacksmith-2vcpu-ubuntu-2404", cron="0 6 * * *"),
        "groom": _lane(runner="blacksmith-4vcpu-ubuntu-2404", cron="0 6 * * *"),
    }


def test_detect_lanes_spec_plan_and_promote() -> None:
    workflows = {
        "spec.yaml": _stub("spec"),
        "plan.yml": _stub("plan", ref="@v2"),
        "promote.yml": _stub("promote", runner="blacksmith-4vcpu-ubuntu-2404"),
    }
    assert detect_lanes(workflows) == {
        "spec": _lane(),
        "plan": _lane(),
        "promote": _lane(runner="blacksmith-4vcpu-ubuntu-2404"),
    }


def test_detect_lanes_scout_stub_cron_no_runner() -> None:
    stub = _stub("scout", cron="0 18 * * *")
    assert detect_lanes({"scout.yml": stub}) == {"scout": _lane(cron="0 18 * * *")}


def test_detect_lanes_yaml_extension_in_uses() -> None:
    content = "jobs:\n  t:\n    uses: o/agent-ops/.github/workflows/triage-pipeline.yaml@main\n"
    assert detect_lanes({"t.yml": content}) == {"triage": _lane()}


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
    assert detect_lanes({"broken.yml": content}) == {"triage": _lane()}


def test_detect_lanes_empty() -> None:
    assert detect_lanes({}) == {}


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


# --- fleet_failures ----------------------------------------------------------


Proc = subprocess.CompletedProcess[str]
FakeRun = Callable[..., Proc]


def _proc(stdout: str = "", *, returncode: int = 0, stderr: str = "") -> Proc:
    return subprocess.CompletedProcess(["gh"], returncode, stdout=stdout, stderr=stderr)


def _run(name: str, branch: str = "main", created: str = "2026-07-26T08:14:22Z") -> dict[str, Any]:
    return {
        "name": name,
        "conclusion": "failure",
        "headBranch": branch,
        "createdAt": created,
        "url": f"https://github.com/o/r/actions/runs/{len(name)}",
    }


def _canned(responses: dict[str, Proc]) -> FakeRun:
    """A `run()` stand-in that answers per the `--repo` argument it is given."""

    def fake(cmd: list[str], **kwargs: Any) -> Proc:
        return responses[cmd[cmd.index("--repo") + 1]]

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
    assert seen == [status.FAILURE_TIMEOUT_S]
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


def test_branch_elides_only_when_it_would_push_the_url_out() -> None:
    assert _branch({"headBranch": "main"}) == "main"
    short = "a" * status.BRANCH_WIDTH
    assert _branch({"headBranch": short}) == short
    long = "jirathip-k/ci-linux-swift-tests-and-more"
    elided = _branch({"headBranch": long})
    assert len(elided) == status.BRANCH_WIDTH
    # Both ends survive: the lane prefix and the issue suffix are what identify it.
    assert elided.startswith("jirathip-k/") and elided.endswith("more")


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
