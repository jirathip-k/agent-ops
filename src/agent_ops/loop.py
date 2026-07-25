from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from agent_ops.config import ProjectConfig
from agent_ops.fallback import pin_to_model, run_with_fallback
from agent_ops.gates import GateResult, format_failures, run_gates
from agent_ops.runtimes import FailureKind, RunRequest, RunResult, Runtime

RETRY_TEMPLATE = """\
A previous attempt at the task below did not pass verification.
Fix the problems listed under "Verification failures", keeping the working tree
changes that are already correct.

## Original task

{task}

## Verification failures

{feedback}
"""


@dataclass(frozen=True)
class LoopOutcome:
    ok: bool
    attempts: int
    last_result: RunResult | None
    gate_failures: list[GateResult]


def run_task_loop(
    runtime: Runtime,
    request: RunRequest,
    config: ProjectConfig,
    cwd: Path,
    on_event: Callable[[str], None] = lambda _: None,
) -> LoopOutcome:
    """Execute-verify-retry loop.

    Each retry is a FRESH session (no resume): the retry prompt restates the
    task plus the failure report. Fresh context avoids anchoring on a broken
    approach and mirrors the CI pipeline's fresh-implementer rule.
    """
    feedback: str | None = None
    last_result: RunResult | None = None
    failures: list[GateResult] = []
    current = request

    for attempt in range(1, config.loop.max_attempts + 1):
        prompt = (
            request.prompt
            if feedback is None
            else RETRY_TEMPLATE.format(task=request.prompt, feedback=feedback)
        )
        on_event(f"attempt {attempt}/{config.loop.max_attempts}: running {runtime.name}")
        last_result = run_with_fallback(runtime, replace(current, prompt=prompt), on_event=on_event)
        # A substitution sticks for the rest of the loop; a gate failure never
        # changes the model, so this only moves when the model itself refused.
        current = pin_to_model(current, last_result.model)

        if not last_result.ok:
            if runtime.classify_failure(last_result) is FailureKind.MODEL_UNAVAILABLE:
                # The ladder is spent — more attempts would hit the same wall.
                on_event("no fallback model left; giving up without further attempts")
                return LoopOutcome(False, attempt, last_result, failures)
            feedback = f"The agent runtime itself failed:\n{last_result.text}"
            on_event("runtime reported an error; retrying")
            continue

        failures = [g for g in run_gates(config, cwd) if not g.ok]
        if not failures:
            on_event(f"all gates passed on attempt {attempt}")
            return LoopOutcome(True, attempt, last_result, [])

        feedback = format_failures(failures)
        on_event(f"gates failed: {', '.join(f.name for f in failures)}")

    return LoopOutcome(False, config.loop.max_attempts, last_result, failures)
