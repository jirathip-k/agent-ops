# agent-ops

Reusable agentic SDLC platform. One repo owns the agent infrastructure —
CLI, workflows, runtime abstraction, worktree isolation, prompts, skills —
while each project repo carries only its own knowledge (`AGENTS.md`,
`.agent/config.yaml`, project skills).

Two lanes share one philosophy (gates before merge, fresh context per retry,
humans own `main`):

| Lane | Entry point | Billing | Use for |
| --- | --- | --- | --- |
| **Local** | `agent` CLI | Claude/Codex subscription | Interactive: implement an issue, review a PR |
| **CI** | `.github/workflows/triage-pipeline.yml` | Subscription OAuth token via `claude-code-action` | Scheduled triage/fix/review across managed repos |

See `docs/architecture.md` for the full picture and `docs/adr/` for why it's
built this way.

## Install

```sh
uv tool install --editable .   # editable: the CLI reads prompts/skills from this repo
agent doctor                   # verifies git, gh, claude (and optional codex)
```

## Onboard a project

```sh
cd ~/Projects/your-app
agent init                     # AGENTS.md + CLAUDE.md symlink + .agent/config.yaml
$EDITOR .agent/config.yaml     # set test/lint/typecheck commands — these are the gates
$EDITOR AGENTS.md              # fill in project knowledge and danger zones
agent doctor                   # confirm gates are configured
```

`AGENTS.md` is canonical; `CLAUDE.md` is symlinked to it so Claude Code and
other runtimes share one instruction file. Existing files are never
overwritten.

`agent init` also prints the label set the lanes use — run those `gh label
create` lines once in the repo. The gate labels (`spec-requested`,
`plan-requested`, `approved-for-agent`) are applied by hand, so the lanes
that select on them stay idle until the labels exist.

## Use it

```sh
agent scout                    # mine TODOs/deferred threads/gaps → file backlog issues (≤3)
agent spec 123                 # backlog idea → checklist acceptance criteria, posted on the issue
agent queue                    # open issues labeled agent-ready, oldest first
agent plan 123 --post          # planner only (smart model, read-only) → issue comment
agent plan 123 --surface orca  # same, but on a visible Orca terminal instead of inline
agent implement 123            # worktree → plan → implement loop → gates → self-review → PR
agent implement 123 --no-pr    # same, but stop before push/PR (good while building trust)
agent resume 123               # rerun the implementer in the existing worktree
agent resume 123 -m "..."      # ...with your feedback instead of the stored self-review
agent spawn 123 -m "..."       # ad-hoc: an interactive agent in the worktree, wired to report back
agent report 123 --state done  # ...what that agent (or its stop hook) reports on the way out
agent review 45                # read-only review of PR #45 (add --post to comment)
agent review 45 --surface orca # same, but on a visible Orca terminal instead of inline
agent review --all             # review every open PR targeting base_branch, concurrently
agent worktree list            # see in-flight task worktrees
agent runs                     # per-issue state: running / halted / stopped / done
agent runs --wait              # block until every tracked run finishes, printing transitions
agent status --sync-orca       # mirror active agent lanes onto Orca cards (read-only)
agent runtimes                 # claude_code / codex availability
```

Stages fan out across roles via model tiers: **planner** and **reviewer**
run the `smart` tier (currently `fable`) in read-only mode; **implementer**
runs the `fast` tier (currently `sonnet`) with write access. Tiers are
defined once in `config/defaults.yaml` (`model_tiers:`) using floating
vendor aliases, so they track new model releases without config changes;
override per project or per role under `agents:` in `.agent/config.yaml`.
A planner `ESCALATE:` stops the workflow before anything is changed. Agent
activity streams live (tool calls + text) by default; set
`runtime.stream: false` for quiet runs.

The full loop — capture (`agent scout` for agent-sourced ideas) → groom →
spec (`agent spec` turns backlog ideas into agent-ready checklists) →
dispatch → review → merge — plus running parallel agents under Orca is
described in `docs/workflow.md`.

The implement loop retries up to `loop.max_attempts` times; each retry is a
fresh session fed the original task plus the gate-failure report. On failure
the worktree is kept for inspection.

Each gate command (and `commands.setup`) is bounded by
`loop.gate_timeout_seconds` — 30 minutes by default; raise it for a slow test
suite. A gate that overruns is reported as a failed gate, so the retry prompt
says so instead of the run hanging. Every other subprocess `agent` shells out
to is bookkeeping (`gh`, `git`) and carries a short built-in bound.

When self-review requests changes, the worktree is kept, the findings are
saved to `.agent-runs/issue-<N>-feedback.md`, and a `## Agent self-review`
comment is posted on the issue as a human-visible marker that it's halted
rather than unstarted (best-effort; a missing `gh` remote won't fail the run).
`agent queue` doesn't read that comment — a halted issue keeps its
`agent-ready` label and still shows up there.
`agent resume <N>` picks that worktree back up: it defaults to the stored
findings, or takes `--message`/`--message-file` to supply different feedback,
and attaches to a surface the same way `agent dispatch` does. Feedback always
reaches the agent via a file, never a shell-interpolated argument.

## CI lane (scheduled triage pipeline)

Each managed repo gets a stub workflow (`stubs/managed-repo-triage.yml`)
calling the reusable pipeline here. The orchestrator prompt
(`prompts/orchestrator.md`) runs Planner → Implementer → Tester → Reviewer
with fresh context per agent. Branch model per managed repo:

    fix/issue-N ──► staging (agent auto-merge, gated) ──► main (human only)
    hotfix/issue-N ──► main (human merge) ──► back-merge to staging

Setup:

1. `bash setup.sh` (git init, create GitHub repo)
2. `claude setup-token` → add as `CLAUDE_CODE_OAUTH_TOKEN` secret (org-level
   if managing multiple repos)
3. Per managed repo: create `staging`, labels, branch protection, copy the
   stub workflow (setup.sh prints the exact commands)
4. Register the repo in `config/repos.yml` with `auto_merge.enabled: false`
   (report-only) for the first week

### Safety gates (enforced in prompt AND GitHub settings)

- Branch protection: `main` requires human-approved PR; `staging` requires
  green checks
- Auto-merge only if: tests PASS, review APPROVE, CI green, diff ≤ 200 lines
  / ≤ 5 files, no touches to CI/auth/migrations/deps/infra
- Hotfixes are never auto-merged; one revision round per stage, then escalate
- Caps: 3 issues per run, 55-minute timeout, concurrency lock

### Operating it

- **Pause one repo:** disable its triage workflow (Actions → ⋯ → Disable)
- **Run manually:** Actions → Hourly Agent Triage → Run workflow
- **Escalations:** anything unsafe gets `needs-human` with an explanation
- **Widen autonomy gradually:** report-only → auto-merge to staging →
  shorter soak. Never let agents merge to `main`.
- **Prompt changes are code changes:** PR them; history is the audit trail.

## Development (of this platform)

```sh
uv sync --dev
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

CI runs the same four checks on every PR. Commit style:
`component: imperative summary`.

## Repo map

```
src/agent_ops/       CLI, config, loop, gates, worktree, github, runtimes/, workflows/
prompts/tasks/       local-lane task prompts (implement, review)
prompts/orchestrator.md + prompts/agents/   CI-lane prompt pipeline
skills/              reusable prompt skills (coding, testing, review, documentation)
templates/project/   what `agent init` writes into a project
config/defaults.yaml platform defaults; config/repos.yml managed-repo registry
stubs/               workflow stub to copy into managed repos
docs/                architecture, ADRs, roadmap, office-ops suggestion
```

## Notes on subscription usage

Local runs and CI runs draw from the same subscription quota as interactive
Claude Code sessions. Subscriptions are intended for single users — for
multi-user or heavy unattended automation, switch the CI lane to API-key
billing. Regenerate with `claude setup-token` if CI runs start failing auth.
