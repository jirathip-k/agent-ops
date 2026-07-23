# Roadmap

Rough order. Each item should land as a PR with tests; promote nothing until
the previous stage is boringly reliable.

## Now (make the vertical slice trustworthy)

- [ ] Run `agent init` + configure `.agent/config.yaml` in one real project;
      close 3–5 real issues through `agent implement` with `--no-pr` first.
- [ ] Register that project in `config/repos.yml` (report-only) and let the
      CI triage lane run for a week before enabling auto-merge to staging.

## Next

- [x] `agent plan <issue>` + in-workflow plan stage (planner role on a
      stronger model, read-only; ESCALATE stops the run).
- [ ] Structured run logs (`.agent-runs/<task-id>.jsonl`): attempts, gate
      results, cost — the audit trail for tuning prompts.
- [ ] `agent implement --from-plan`: consume an approved plan comment
      instead of re-planning.
- [ ] Codex adapter parity: JSONL event parsing, verdict extraction.
- [ ] Batch mode: `agent triage` running the CI orchestrator logic locally.

## Later / explicitly deferred

- **Memory subsystem** — deferred (ADR 0003). Reconsider only if editing
  AGENTS.md/skills by hand becomes the bottleneck; likely shape: agent
  proposes AGENTS.md edits as PRs after repeated failures.
- **LLM-as-evaluator beyond review** — deterministic gates first.
- **Additional runtimes (Gemini CLI, OpenCode)** — one adapter file each,
  when needed.
- **Office automation** — separate repo; see docs/office-ops.md.
