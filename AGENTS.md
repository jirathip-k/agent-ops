# AGENTS.md

Instructions for coding agents working in this repository.

## What this project is

agent-ops is a reusable agentic SDLC platform: a Python CLI (`agent`) that
orchestrates coding agents (Claude Code, Codex) through plan → implement →
gate → review workflows in isolated git worktrees, plus a prompt-driven CI
triage pipeline. This repo manages itself with its own tooling.

## Architecture

- `src/agent_ops/cli.py` — typer CLI, thin wiring only
- `src/agent_ops/workflows/` — business logic (implement, review)
- `src/agent_ops/runtimes/` — `Runtime` protocol + CLI adapters; workflows
  must depend only on the protocol in `base.py`
- `src/agent_ops/{config,loop,gates,worktree,github,skills,prompts}.py` —
  one concern per module
- `prompts/tasks/` — local-lane prompt templates (`{placeholder}` format)
- `prompts/orchestrator.md` + `prompts/agents/` — CI-lane pipeline
- Decisions are recorded in `docs/adr/` — read them before changing
  direction (e.g. no memory store, shell-out-to-CLIs, state in GitHub)

## Conventions

- Python 3.12, `from __future__ import annotations`, full type hints;
  pyright standard mode must stay at 0 errors
- ruff for lint + format (line length 100); no new dependencies without
  strong justification
- pydantic models for config; dataclasses for plain value objects
- Subprocesses go through `utils.run()` — never raw `subprocess.run`
  (exception: runtime adapters may use `Popen` for streaming output)
- Commit style: `component: imperative summary` (e.g. `cli: add plan command`)

## Commands

- Test: `uv run pytest -q`
- Lint: `uv run ruff check . && uv run ruff format --check .`
- Typecheck: `uv run pyright`

## Danger zones

- `.github/workflows/` — CI and the reusable triage pipeline; never modify
  in an automated change
- `prompts/orchestrator.md` safety rules and `config/defaults.yaml` safety
  defaults (auto-merge caps, blocked paths) — human-reviewed changes only
- `pyproject.toml` dependencies and `uv.lock`
