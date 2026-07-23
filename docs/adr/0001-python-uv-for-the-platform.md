# ADR 0001: Python + uv for the platform CLI

**Status:** accepted · 2026-07-23

## Context

The platform orchestrates subprocesses (`claude`, `codex`, `gh`, `git`) and
merges YAML config. Candidates: Python (uv/ruff/pyright) or TypeScript (both
in our primary stacks).

## Decision

Python 3.12, uv-managed, typer CLI, pydantic config models, src layout.
Installed with `uv tool install --editable .` so prompts/skills/config resolve
to the checked-out repo.

## Consequences

- Orchestration-heavy, SDK-light code stays small and typed; pyright standard
  mode and ruff run in CI.
- Editable install is required (the platform reads its own repo files at
  runtime). If we ever distribute this, move prompts/skills to package data.
- If we later want the Claude Agent SDK's programmatic features, Anthropic
  ships a Python SDK too — no language switch needed.
