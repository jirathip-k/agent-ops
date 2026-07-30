from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

from agent_ops.runtimes.base import FailureKind, RunRequest, RunResult, Runtime


class ModelUnavailableError(RuntimeError):
    """No configured model or provider can answer this queue.

    Distinct from a plain run failure so callers that fan out multiple runs
    (e.g. `agent review --all`) can tell an exhausted model ladder or provider
    chain from one bad run and abort the rest of the queue instead of burning
    it on the same wall.
    """


@dataclass(frozen=True)
class ProviderRuntime:
    """One provider and its independently resolved model ladder."""

    runtime: Runtime
    model: str | None
    fallback_models: tuple[str, ...] = ()


class RuntimeChain:
    """A per-run ordered provider chain that stays pinned after failover.

    The object implements Runtime so workflows and the feedback loop keep
    depending on the same protocol. Routing still lives in run_with_fallback:
    calling an adapter directly never gains hidden fallback behaviour.
    """

    def __init__(self, providers: list[ProviderRuntime]) -> None:
        if not providers:
            raise ValueError("a runtime chain needs at least one provider")
        self.providers = tuple(providers)
        self._active_index = 0
        self._configured_provider = providers[0].runtime.name
        self._configured_model = providers[0].model

    @property
    def name(self) -> str:
        return self.providers[self._active_index].runtime.name

    @property
    def configured_provider(self) -> str:
        return self._configured_provider

    @property
    def configured_model(self) -> str | None:
        return self._configured_model

    @property
    def active_provider(self) -> ProviderRuntime:
        return self.providers[self._active_index]

    @property
    def active_index(self) -> int:
        return self._active_index

    def pin(self, index: int) -> None:
        self._active_index = index

    def pin_model(self, provider_name: str | None, model: str | None) -> None:
        """Keep a model substitution on the selected provider for later retries."""
        for index, provider in enumerate(self.providers):
            if provider.runtime.name != provider_name:
                continue
            ladder = [provider.model, *provider.fallback_models]
            if model not in ladder:
                return
            remaining = tuple(
                rung for rung in ladder[ladder.index(model) + 1 :] if rung is not None
            )
            updated = replace(provider, model=model, fallback_models=remaining)
            self.providers = (
                *self.providers[:index],
                updated,
                *self.providers[index + 1 :],
            )
            return

    def available(self) -> bool:
        return any(provider.runtime.available() for provider in self.providers)

    def request_for(self, request: RunRequest, index: int) -> RunRequest:
        provider = self.providers[index]
        return replace(
            request,
            model=provider.model,
            fallback_models=provider.fallback_models,
        )

    def run(self, request: RunRequest) -> RunResult:
        """Run only the pinned provider; callers use run_with_fallback for routing."""
        provider = self.active_provider
        return provider.runtime.run(self.request_for(request, self._active_index))

    def classify_failure(self, result: RunResult) -> FailureKind:
        raw = result.raw or {}
        if raw.get("failure_kind") == FailureKind.PROVIDER_UNAVAILABLE:
            return FailureKind.PROVIDER_UNAVAILABLE
        provider = next(
            (
                candidate
                for candidate in self.providers
                if candidate.runtime.name == result.provider
            ),
            self.active_provider,
        )
        return provider.runtime.classify_failure(result)


def model_ladder(request: RunRequest) -> list[str | None]:
    """The configured model first, then each fallback rung in order."""
    return [request.model, *request.fallback_models]


def run_with_fallback(
    runtime: Runtime,
    request: RunRequest,
    on_event: Callable[[str], None] = lambda _: None,
) -> RunResult:
    """Run `request`, walking model ladders and then an ordered provider chain.

    Adaptation on failure, not a downgrade: with no ladder configured, or with
    a model that answers, this is exactly one `runtime.run` call and nothing
    else happens. Only explicit model/provider unavailability advances — a
    transient error or an ordinary agent failure is returned untouched so the
    caller's own retry policy (and the CLI's own backoff) decides what to do.

    The returned result carries the provider and model that actually produced
    it, so callers can attribute whatever artifact they publish.
    """
    if isinstance(runtime, RuntimeChain):
        return _run_chain(runtime, request, on_event)
    return _run_provider(
        runtime,
        request,
        configured_provider=runtime.name,
        configured_model=request.model,
        on_event=on_event,
    )


def _run_chain(
    chain: RuntimeChain,
    request: RunRequest,
    on_event: Callable[[str], None],
) -> RunResult:
    configured_provider = chain.configured_provider
    configured_model = chain.configured_model
    for index in range(chain.active_index, len(chain.providers)):
        provider = chain.providers[index]
        provider_name = provider.runtime.name
        chain.pin(index)
        if not provider.runtime.available():
            result = RunResult(
                ok=False,
                text=f"Runtime {provider_name!r} CLI is not installed/on PATH",
                raw={"failure_kind": FailureKind.PROVIDER_UNAVAILABLE},
                model=provider.model,
                provider=provider_name,
                configured_provider=configured_provider,
                configured_model=configured_model,
            )
            if index + 1 >= len(chain.providers):
                on_event(
                    f"PROVIDER FALLBACK exhausted: {provider_name!r} is unavailable and "
                    "no provider is left"
                )
                return result
            next_name = chain.providers[index + 1].runtime.name
            on_event(f"PROVIDER FALLBACK: {provider_name!r} is unavailable — trying {next_name!r}")
            continue

        result = _run_provider(
            provider.runtime,
            chain.request_for(request, index),
            configured_provider=configured_provider,
            configured_model=configured_model,
            on_event=on_event,
        )
        kind = provider.runtime.classify_failure(result) if not result.ok else None
        if result.ok or kind not in {
            FailureKind.MODEL_UNAVAILABLE,
            FailureKind.PROVIDER_UNAVAILABLE,
        }:
            chain.pin_model(result.provider, result.model)
            return result
        if index + 1 >= len(chain.providers):
            on_event(
                f"PROVIDER FALLBACK exhausted: {provider_name!r} is unavailable and "
                "no provider is left"
            )
            return result
        next_name = chain.providers[index + 1].runtime.name
        on_event(
            f"PROVIDER FALLBACK: {provider_name!r} exhausted its model ladder — "
            f"trying {next_name!r}. Output will come from a different provider."
        )
    raise AssertionError("unreachable: a runtime chain always has an active provider")


def _run_provider(
    runtime: Runtime,
    request: RunRequest,
    *,
    configured_provider: str,
    configured_model: str | None,
    on_event: Callable[[str], None],
) -> RunResult:
    ladder = model_ladder(request)
    for index, model in enumerate(ladder):
        attempt = request if model == request.model else replace(request, model=model)
        result = replace(
            runtime.run(attempt),
            model=model,
            provider=runtime.name,
            configured_provider=configured_provider,
            configured_model=configured_model,
        )
        # A provider-wide refusal skips the rest of this provider immediately;
        # only model-specific unavailability has a meaningful next model rung.
        if result.ok or runtime.classify_failure(result) is not FailureKind.MODEL_UNAVAILABLE:
            return result

        remaining = ladder[index + 1 :]
        if not remaining:
            # Loud on the way out too: an exhausted ladder is a config gap, and
            # the operator needs to see which rungs were actually tried.
            on_event(
                f"MODEL FALLBACK [{runtime.name}] exhausted: {_name(model)} is unavailable "
                "and no rung is left "
                f"(tried {', '.join(_name(m) for m in ladder)})"
            )
            return result
        on_event(
            f"MODEL FALLBACK [{runtime.name}]: {_name(model)} is unavailable — retrying on "
            f"{_name(remaining[0])}. Output will come from a different model than configured."
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
    """Name the provider/model that produced `result`, flagging substitutions.

    A review written by a fallback model is a materially different review, so
    every artifact a run publishes says which model wrote it.
    """
    used = result.model or request.model
    provider = result.provider or result.configured_provider or "unknown"
    configured_provider = result.configured_provider or provider
    configured_model = request.model
    if provider != configured_provider and result.configured_provider is not None:
        configured_model = result.configured_model
    model = used or "runtime default"
    if provider != configured_provider or used != configured_model:
        configured = configured_model or "runtime default"
        return (
            f"provider: {provider}, model: {model} "
            f"(FALLBACK — configured {configured_provider} / {configured} was unavailable)"
        )
    return f"provider: {provider}, model: {model}"


def artifact_footer(request: RunRequest, result: RunResult) -> str:
    """Attribution appended to anything the run posts to GitHub."""
    return f"\n\n---\n_agent-ops · {model_note(request, result)}_"


def _name(model: str | None) -> str:
    return repr(model) if model is not None else "the runtime default model"
