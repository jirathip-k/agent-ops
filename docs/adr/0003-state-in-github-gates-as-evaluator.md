# ADR 0003: State lives in GitHub; gates are the evaluator; no memory store

**Status:** accepted · 2026-07-23

## Context

The reference architecture sketches planner/loops/evaluator/memory
subsystems. Building all four up front risks a framework nobody uses.

## Decision

- **State**: issues, labels, branches, PR status — GitHub is the database.
  Runs are stateless and resumable by re-reading GitHub.
- **Evaluator**: deterministic gates (project test/lint/typecheck commands)
  plus one read-only review agent. A judgement is either a command's exit
  code or a reviewer's explicit verdict line.
- **Memory**: none. Durable knowledge goes into `AGENTS.md` and skills files
  via normal PRs — human-reviewed, versioned, auditable.

## Consequences

- Zero infrastructure to operate; everything is inspectable in the GitHub UI.
- "Learning" is deliberate (edit a skill file) rather than automatic; revisit
  if that becomes the bottleneck (see docs/roadmap.md).
- Retries use fresh context + failure report instead of session resume,
  mirroring the CI pipeline's fresh-implementer rule.
