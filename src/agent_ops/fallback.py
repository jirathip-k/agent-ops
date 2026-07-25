from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult, Runtime


class ModelUnavailableError(RuntimeError):
    """The model ladder is exhausted: no configured or fallback model answered.

    Distinct from a plain run failure so callers that fan out multiple runs
    (e.g. `agent review --all`) can tell a config-wide gap apart from one bad
    run and abort the rest of the queue instead of burning it on the same wall.
    """


def model_ladder(request: RunRequest) -> list[str | None]:
    """The configured model first, then each fallback rung in order."""
    return [request.model, *request.fallback_models]


def run_with_fallback(
    runtime: Runtime,
    request: RunRequest,
    on_event: Callable[[str], None] = lambda _: None,
) -> RunResult:
    """Run `request`, stepping down the model ladder when a model is unavailable.

    Adaptation on failure, not a downgrade: with no ladder configured, or with
    a model that answers, this is exactly one `runtime.run` call and nothing
    else happens. Only FailureKind.MODEL_UNAVAILABLE advances a rung — a
    transient error or an ordinary agent failure is returned untouched so the
    caller's own retry policy (and the CLI's own backoff) decides what to do.

    The returned result carries the model that actually produced it, so callers
    can attribute whatever artifact they publish.
    """
    ladder = model_ladder(request)
    for index, model in enumerate(ladder):
        attempt = request if model == request.model else replace(request, model=model)
        result = replace(runtime.run(attempt), model=model)
        if result.ok or runtime.classify_failure(result) is not FailureKind.MODEL_UNAVAILABLE:
            return result

        remaining = ladder[index + 1 :]
        if not remaining:
            # Loud on the way out too: an exhausted ladder is a config gap, and
            # the operator needs to see which rungs were actually tried.
            on_event(
                f"MODEL FALLBACK exhausted: {_name(model)} is unavailable and no rung is left "
                f"(tried {', '.join(_name(m) for m in ladder)})"
            )
            return result
        on_event(
            f"MODEL FALLBACK: {_name(model)} is unavailable — retrying on {_name(remaining[0])}. "
            "Output will come from a different model than configured."
        )
    raise AssertionError("unreachable: the ladder always has at least one rung")


def pin_to_model(request: RunRequest, model: str | None) -> RunRequest:
    """Restrict `request` to `model` and the rungs below it.

    A substitution holds for the rest of the run: once fable has refused, later
    attempts in the same loop must not walk back up into it and burn a call per
    attempt rediscovering the same limit.
    """
    ladder = model_ladder(request)
    if model not in ladder:
        return request
    remaining = [rung for rung in ladder[ladder.index(model) + 1 :] if rung is not None]
    return replace(request, model=model, fallback_models=tuple(remaining))


def model_note(request: RunRequest, result: RunResult) -> str:
    """One line naming the model that produced `result`, flagging substitutions.

    A review written by a fallback model is a materially different review, so
    every artifact a run publishes says which model wrote it.
    """
    used = result.model or request.model
    if used is None:
        return "model: runtime default"
    if request.model and used != request.model:
        return f"model: {used} (FALLBACK — configured {request.model} was unavailable)"
    return f"model: {used}"


def artifact_footer(request: RunRequest, result: RunResult) -> str:
    """Attribution appended to anything the run posts to GitHub."""
    return f"\n\n---\n_agent-ops · {model_note(request, result)}_"


def _name(model: str | None) -> str:
    return repr(model) if model is not None else "the runtime default model"
