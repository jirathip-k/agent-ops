from __future__ import annotations

import os
import shlex
from pathlib import Path

import pytest

from agent_ops import github
from agent_ops.config import ProjectConfig
from agent_ops.gates import GateStatus, run_gates
from agent_ops.loop import run_task_loop
from agent_ops.runtimes import FailureKind, RunRequest, RunResult
from agent_ops.workflows import implement as implement_module
from agent_ops.workflows.implement import task_role_request
from agent_ops.workflows.review import run_review

BEGIN_CONTRACT = "<!-- BEGIN AGENT-OPS CONFIGURED EXECUTABLE CONTRACT -->"
END_CONTRACT = "<!-- END AGENT-OPS CONFIGURED EXECUTABLE CONTRACT -->"

ECOSYSTEMS = {
    "pytest": {
        "commands": {
            "setup": "uv sync --dev",
            "test": "uv run pytest -q",
            "lint": "uv run ruff check .",
            "typecheck": "uv run pyright",
        },
        "gate_argv": [
            "uv run pytest -q",
            "uv run ruff check .",
            "uv run pyright",
        ],
    },
    "npm": {
        "commands": {
            "setup": "npm ci",
            "test": "npm run test",
            "lint": "npm run lint",
            "typecheck": "npm run typecheck",
        },
        "gate_argv": [
            "npm run test",
            "npm run lint",
            "npm run typecheck",
        ],
    },
    "swift": {
        "commands": {
            "setup": "swift package resolve",
            "test": "swift test",
            "lint": "swift format lint --recursive .",
            "typecheck": "swift build",
        },
        "gate_argv": [
            "swift test",
            "swift format lint --recursive .",
            "swift build",
        ],
    },
}

TASKS = {
    "implement": (
        "implementer",
        {
            "issue_number": "300",
            "issue_title": "configured gates",
            "issue_labels": "bug",
            "issue_body": "Repository docs may mention an alternate command.",
            "branch": "fix/issue-300",
            "plan": "Use the configured command.",
            "authorization": "(none)",
            "skills": "",
        },
    ),
    "resume": (
        "implementer",
        {
            "issue_number": "300",
            "issue_title": "configured gates",
            "issue_labels": "bug",
            "issue_body": "Repository docs may mention an alternate command.",
            "branch": "fix/issue-300",
            "diff_stat": "one file changed",
            "feedback": "Run the configured test.",
            "ci_status": "",
            "authorization": "(none)",
            "skills": "",
        },
    ),
    "plan": (
        "planner",
        {
            "issue_number": "300",
            "issue_title": "configured gates",
            "issue_labels": "bug",
            "issue_body": "Repository docs may mention an alternate command.",
            "issue_comments": "(no comments)",
            "authorization": "(none)",
        },
    ),
    "review": (
        "reviewer",
        {
            "context": "Pre-commit review.",
            "diff": "diff --git a/a.py b/a.py\n",
        },
    ),
}


class _AvailableRuntime:
    name = "fake"

    def __init__(self) -> None:
        self.requests: list[RunRequest] = []

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest) -> RunResult:
        self.requests.append(request)
        return RunResult(ok=True, text="VERDICT: APPROVE")

    def classify_failure(self, result: RunResult) -> FailureKind:
        return FailureKind.AGENT_FAILURE


@pytest.mark.parametrize("ecosystem", ECOSYSTEMS)
def test_each_ecosystem_executes_every_task_request_and_exact_allowlist(
    ecosystem: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = ECOSYSTEMS[ecosystem]
    commands = fixture["commands"]
    config = ProjectConfig.model_validate({"commands": commands})
    monkeypatch.setattr(implement_module, "get_runtime", lambda _name: _AvailableRuntime())

    expected_allowed = tuple(
        pattern
        for command in commands.values()
        for pattern in (f"Bash({command})", f"Bash({command}:*)")
    )

    for task_name, (role_name, fields) in TASKS.items():
        _runtime, request = task_role_request(
            config,
            role_name,
            task_name,
            tmp_path,
            fields,
        )
        contract = request.prompt.split(BEGIN_CONTRACT, 1)[1].split(END_CONTRACT, 1)[0]

        assert request.allowed_tools == expected_allowed
        assert contract.strip().splitlines() == [
            f"{name}: {command}" for name, command in commands.items()
        ]
        assert "sole executable contract" in request.prompt
        assert (
            "parent `run_gates` execution remains the final pass/fail authority" in request.prompt
        )


def test_npm_documented_alias_does_not_replace_configured_request_or_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = ProjectConfig.model_validate({"commands": ECOSYSTEMS["npm"]["commands"]})
    monkeypatch.setattr(implement_module, "get_runtime", lambda _name: _AvailableRuntime())
    fields = dict(TASKS["implement"][1])
    fields["issue_body"] = "CLAUDE.md documents `npm test`."

    _runtime, request = task_role_request(
        config,
        "implementer",
        "implement",
        tmp_path,
        fields,
    )
    contract = request.prompt.split(BEGIN_CONTRACT, 1)[1].split(END_CONTRACT, 1)[0]

    assert "CLAUDE.md documents `npm test`." in request.prompt
    assert "test: npm run test" in contract
    assert "test: npm test" not in contract
    assert "Bash(npm run test)" in request.allowed_tools
    assert "Bash(npm run test:*)" in request.allowed_tools
    assert "Bash(npm test)" not in request.allowed_tools
    assert "Bash(npx vitest)" not in request.allowed_tools


def test_standalone_reviewer_request_receives_the_same_npm_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        "commands:\n"
        "  setup: npm ci\n"
        "  test: npm run test\n"
        "  lint: npm run lint\n"
        "  typecheck: npm run typecheck\n"
    )
    runtime = _AvailableRuntime()
    monkeypatch.setattr(implement_module, "get_runtime", lambda _name: runtime)
    monkeypatch.setattr(
        github,
        "pr_view",
        lambda _number, cwd: {"number": 42, "title": "gate fix", "body": ""},
    )
    monkeypatch.setattr(
        github,
        "pr_diff",
        lambda _number, cwd: "diff --git a/a.py b/a.py\n+fixed\n",
    )

    assert run_review(tmp_path, 42, log=lambda _message: None) == "VERDICT: APPROVE"

    request = runtime.requests[0]
    contract = request.prompt.split(BEGIN_CONTRACT, 1)[1].split(END_CONTRACT, 1)[0]
    assert "test: npm run test" in contract
    assert request.allowed_tools == (
        "Bash(npm ci)",
        "Bash(npm ci:*)",
        "Bash(npm run test)",
        "Bash(npm run test:*)",
        "Bash(npm run lint)",
        "Bash(npm run lint:*)",
        "Bash(npm run typecheck)",
        "Bash(npm run typecheck:*)",
    )


@pytest.mark.parametrize("ecosystem", ECOSYSTEMS)
def test_each_ecosystem_fixture_executes_the_exact_parent_gate_strings(
    ecosystem: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = ECOSYSTEMS[ecosystem]
    commands = fixture["commands"]
    capture = tmp_path / "gate-argv.txt"
    monkeypatch.setenv("GATE_CAPTURE", str(capture))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")

    for command in commands.values():
        binary = shlex.split(command)[0]
        shim = tmp_path / binary
        if not shim.exists():
            shim.write_text(
                '#!/bin/sh\nprintf "%s %s\\n" "$(basename "$0")" "$*" >> "$GATE_CAPTURE"\n'
            )
            shim.chmod(0o755)

    config = ProjectConfig.model_validate({"commands": commands})
    results = run_gates(config, tmp_path)

    assert [(result.name, result.command, result.status) for result in results] == [
        ("test", commands["test"], GateStatus.PASSED),
        ("lint", commands["lint"], GateStatus.PASSED),
        ("typecheck", commands["typecheck"], GateStatus.PASSED),
    ]
    assert capture.read_text().splitlines() == fixture["gate_argv"]


class _ClaimsDeniedGatePassed:
    name = "claiming-runtime"

    def available(self) -> bool:
        return True

    def run(self, request: RunRequest) -> RunResult:
        return RunResult(ok=True, text="The configured test passed.")

    def classify_failure(self, result: RunResult) -> FailureKind:
        return FailureKind.AGENT_FAILURE


def test_denied_command_cannot_be_reported_as_a_parent_gate_pass(tmp_path: Path) -> None:
    denied = tmp_path / "denied-test"
    denied.write_text("#!/bin/sh\nexit 0\n")
    denied.chmod(0o644)
    config = ProjectConfig.model_validate(
        {
            "commands": {"test": "./denied-test"},
            "loop": {"gates": ["test"], "max_attempts": 1},
        }
    )
    events: list[str] = []

    outcome = run_task_loop(
        _ClaimsDeniedGatePassed(),
        RunRequest(prompt="verify it", cwd=tmp_path),
        config,
        tmp_path,
        on_event=events.append,
    )

    assert outcome.ok is False
    assert outcome.gate_failures[0].status is GateStatus.FAILED
    assert outcome.gate_failures[0].command == "./denied-test"
    assert "Permission denied" in outcome.gate_failures[0].output
    assert not any("all gates passed" in event for event in events)
