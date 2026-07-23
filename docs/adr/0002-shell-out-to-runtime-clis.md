# ADR 0002: Runtimes are CLIs we shell out to, not SDKs we embed

**Status:** accepted · 2026-07-23

## Context

Loops must not be coupled to any one coding agent. Options: embed each
vendor's SDK, or treat each agent's headless CLI (`claude -p`,
`codex exec`) as the integration surface.

## Decision

A small `Runtime` protocol (`run(RunRequest) -> RunResult`) implemented by
adapters that spawn the vendor CLI and parse its output. `claude_code` parses
`--output-format json`; `codex` is a minimal experimental adapter.

## Consequences

- Swapping runtimes is a config value (`runtime.name`); planner/loops never
  change. Adding Gemini CLI etc. is one new adapter file.
- Local runs bill to subscriptions (the CLIs' auth), which is the point:
  interactive work should not burn API credits.
- We accept the CLIs' output contracts as our API; a breaking CLI change
  breaks an adapter, not the platform. Adapter parsing is unit-tested against
  fixture output.
