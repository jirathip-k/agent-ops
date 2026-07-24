# Architecture

agent-ops separates **agent infrastructure** (this repo) from **project
knowledge** (each managed repo's `AGENTS.md` + `.agent/`).

## Layers

```
CLI (agent scout / spec / triage / groom / plan / implement / review / ...)
 │
 ├─ config      platform defaults ⊕ project .agent/config.yaml
 ├─ workflows   scout, spec, triage, groom, implement, review   ← business logic
 │    │
 │    ├─ roles      planner / implementer / reviewer — per-role model +
 │    │             permission overrides (agents: in config); planner and
 │    │             reviewer default to a stronger model, read-only
 │    │
 │    ├─ worktree   one isolated worktree + branch per task
 │    ├─ loop       execute → gates → retry (fresh context per retry)
 │    ├─ gates      project test/lint/typecheck commands = the evaluator
 │    ├─ skills     markdown fragments injected into prompts
 │    └─ github     thin `gh` wrappers (issues, PRs, comments)
 │
 └─ runtimes    Runtime protocol
        ├─ claude_code   `claude -p --output-format json`  (implemented)
        └─ codex         `codex exec`                      (experimental)
```

Workflows and the loop depend only on the `Runtime` protocol
(`src/agent_ops/runtimes/base.py`) — swapping runtimes never touches them.

## Two lanes

**Local lane** (`agent` CLI): interactive development on your machine, billed
to your Claude/Codex subscription. Issue → worktree → plan (smart model,
read-only) → implement loop (workhorse model) → self-review (smart model,
read-only) → PR. Each stage is a separate agent with fresh context; the plan
is the only artifact handed forward, mirroring the CI lane's
Planner → Implementer → Reviewer pipeline.

**CI lane** (`.github/workflows/triage-pipeline.yml`): scheduled, unattended
triage across managed repos via `claude-code-action` and the prompt pipeline
in `prompts/orchestrator.md` (Planner → Implementer → Tester → Reviewer).
State lives in GitHub itself: labels, branches, PR status. Runs are stateless.

The lanes share the same philosophy — gates before merge, fresh context per
retry, humans own `main` — but not code paths: the CI lane is prompt-driven
so it runs anywhere `claude-code-action` runs.

## Where things live

| Concern | Location |
| --- | --- |
| Platform defaults | `config/defaults.yaml` |
| Per-project config | `<project>/.agent/config.yaml` |
| Project knowledge | `<project>/AGENTS.md`, `<project>/.agent/skills/` |
| Reusable skills | `skills/*.md` |
| Local task prompts | `prompts/tasks/*.md` |
| CI pipeline prompts | `prompts/orchestrator.md`, `prompts/agents/*.md` |
| Managed-repo registry | `config/repos.yml` |

## Trust boundaries

- Agents write only in worktrees; the platform performs all git operations
  (commit, push, PR) after gates pass.
- Review runs use `permission_mode=plan` (read-only).
- Merges to `main` are always human. See `README.md` safety gates for the CI
  lane's auto-merge conditions.
