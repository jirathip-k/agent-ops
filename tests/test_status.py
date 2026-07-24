from agent_ops.status import LaneInfo, _cell, _short_runner, bucket_counts, detect_lanes


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
