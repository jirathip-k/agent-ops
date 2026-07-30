from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from agent_ops.config import ProjectConfig
from agent_ops.fallback import pin_to_model, run_with_fallback
from agent_ops.gates import GateResult, GateStatus, format_failures, run_gates
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
    missing_gate: GateResult | None = None
    """Set when the loop aborted on a `GateStatus.MISSING` result instead of retrying.

    A trailing field with a default so every existing positional
    `LoopOutcome(...)` construction across the test suite keeps working
    unchanged. `None` in every other outcome, including a plain gate failure
    — that path is unchanged and still retries via `gate_failures`.
    """
    gate_results: tuple[GateResult, ...] = ()
    """Latest complete gate observation, including passes.

    Self-review runs only after a successful loop, so retaining the passing
    results lets the parent workflow give the reviewer the commands and
    verdicts it actually observed. A tuple keeps that handoff frozen.
    """


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
    latest_gate_results: tuple[GateResult, ...] = ()
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
            failure_kind = runtime.classify_failure(last_result)
            if failure_kind in {
                FailureKind.MODEL_UNAVAILABLE,
                FailureKind.PROVIDER_UNAVAILABLE,
            }:
                # The model ladder or provider chain is spent — more attempts
                # would hit the same wall.
                exhausted = "model" if failure_kind is FailureKind.MODEL_UNAVAILABLE else "provider"
                on_event(f"no fallback {exhausted} left; giving up without further attempts")
                return LoopOutcome(False, attempt, last_result, failures)
            feedback = f"The agent runtime itself failed:\n{last_result.text}"
            on_event("runtime reported an error; retrying")
            continue

        results = run_gates(config, cwd)
        latest_gate_results = tuple(results)
        missing = next((g for g in results if g.status is GateStatus.MISSING), None)
        if missing is not None:
            # A binary that isn't on PATH is an environment gap the implementer
            # cannot fix by rewriting code — retrying would just burn the rest
            # of the attempt budget on the same wall (issue #287). Abort on the
            # first occurrence instead of consuming this attempt as a failure.
            binary = missing.missing_binary or "a required binary"
            on_event(
                f"gate {missing.name!r} did not run — {binary} not on PATH; "
                "aborting without retrying"
            )
            return LoopOutcome(
                False,
                attempt,
                last_result,
                [],
                missing_gate=missing,
                gate_results=latest_gate_results,
            )

        failures = [g for g in results if g.status is GateStatus.FAILED]
        if not failures:
            on_event(f"all gates passed on attempt {attempt}")
            return LoopOutcome(True, attempt, last_result, [], gate_results=latest_gate_results)

        feedback = format_failures(failures)
        on_event(f"gates failed: {', '.join(f.name for f in failures)}")

    return LoopOutcome(
        False,
        config.loop.max_attempts,
        last_result,
        failures,
        gate_results=latest_gate_results,
    )
