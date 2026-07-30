# Architecture

agent-ops separates **agent infrastructure** (this repo) from **project
knowledge** (each managed repo's `AGENTS.md` + `.agent/`).

## Layers

```
CLI (agent scout / distill / spec / triage / groom / plan / implement / review / ...)
 │
 ├─ config      platform defaults ⊕ project .agent/config.yaml
 ├─ workflows   scout, distill, spec, triage, groom, implement, review,  ← business logic
 │              spawn
 │    │
 │    ├─ roles      planner / implementer / reviewer — per-role model +
 │    │             permission overrides (agents: in config); planner and
 │    │             reviewer default to a stronger model, read-only
 │    │
 │    ├─ worktree   one isolated worktree + branch per task
 │    ├─ loop       execute → gates → retry (fresh context per retry)
 │    ├─ gates      exact .agent/config.yaml commands = the evaluator
 │    ├─ skills     markdown fragments injected into prompts
 │    └─ github     thin `gh` wrappers (issues, PRs, comments)
 │
 └─ runtimes    Runtime protocol
        ├─ claude_code   `claude -p --output-format json`  (implemented)
        └─ codex         `codex exec`                      (experimental)
```

Workflows and the loop depend only on the `Runtime` protocol
(`src/agent_ops/runtimes/base.py`) — swapping runtimes never touches them.
`agent spawn` needs a larger promise (start the CLI as a session someone
watches, and hook that session's end) and depends on `SpawnableRuntime`, the
protocol that extends it — so a runtime that only knows the headless path
remains a valid `Runtime`.

## Two lanes

**Local lane** (`agent` CLI): interactive development on your machine, billed
to your Claude/Codex subscription. Issue → worktree → plan (smart model,
read-only) → implement loop (workhorse model) → self-review (smart model,
read-only) → PR. Each stage is a separate agent with fresh context; the plan
is the only artifact handed forward, mirroring the CI lane's
Planner → Implementer → Reviewer pipeline.

**CI lane** (`.github/workflows/*-pipeline.yml` — triage, groom, scout, spec,
plan, promote): scheduled, unattended work across managed repos. Only triage
runs via `claude-code-action` and the prompt pipeline in
`prompts/orchestrator.md` (Planner → Implementer → Tester → Reviewer);
groom, scout, spec, plan, and promote each run the matching `agent <verb>`
CLI directly in Actions instead. State lives in GitHub itself: labels,
branches, PR status. Runs are stateless.

The lanes share the same philosophy — gates before merge, fresh context per
retry, humans own `main`. Code paths mostly converge too: five of the six CI
pipelines run the local lane's CLI outright. Triage is the partial exception
— it's prompt-driven, so it runs anywhere `claude-code-action` runs, but its
merge gate calls `agent merge --check` rather than judging caps in prose
(#150), so even there the rules come from one tested place. #171 tracks
converging what's left: triage's classification, review, and implement.

For the local lane, resolved `commands.setup/test/lint/typecheck` values are
one executable contract shared by setup, requirement preflight, agent prompt
rendering, Claude permission patterns, and the parent gate runner. Repository
instruction files remain authoritative for conventions and safety, but cannot
substitute an alternate command spelling. Implement/resume agents execute the
configured gates before finishing for early feedback; the parent gate runner
executes them independently and remains the evaluator.

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
