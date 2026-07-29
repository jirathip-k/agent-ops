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
| **CI** | `.github/workflows/*-pipeline.yml` (six pipelines — see below) | Subscription OAuth token (triage via `claude-code-action`; the rest via the `agent` CLI directly) | Scheduled triage/groom/scout/spec/plan/promote across managed repos |

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

`agent init` also syncs the label set the lanes use (create-or-update, once
per repo — `--print-labels` shows the `gh` commands instead of applying
them). Of the gate labels, `spec-requested` and `plan-requested` can now be
applied by `agent groom` itself (locally or on its daily CI run) as well
as by hand; `approved-for-agent` remains human-only.

## Use it

```sh
agent                          # pipeline TUI: one screen for status/runs/PRs, dispatch+resume,
                                # every keybinding shows the command it runs (also: `agent tui`)
agent scout                    # mine TODOs/deferred threads/gaps → file backlog issues (≤3)
agent triage                   # classify untriaged issues: agent-ready / needs-human / backlog
agent groom                    # re-validate open issues, promote workable ones, apply spec/plan-requested
agent distill                  # prune a grown AGENTS.md: cut spent narration, keep durable notes
agent spec 123                 # backlog idea → checklist acceptance criteria, posted on the issue
agent queue                    # open issues labeled agent-ready, oldest first
agent plan 123 --post          # planner only (smart model, read-only) → issue comment
agent plan 123 --surface orca  # same, but on a visible Orca terminal instead of inline
agent dispatch 123             # the normal way to start work: spawn implement on a visible surface
agent implement 123            # worktree → plan → implement loop → gates → self-review → PR
agent implement 123 --no-pr    # same, but stop before push/PR (good while building trust)
agent claim 123                # mark an issue as being worked on by hand, so other lanes skip it
agent resume 123               # rerun the implementer in the existing worktree
agent resume 123 -m "..."      # ...with your feedback instead of the stored self-review
agent dispatch 123 --grant-file grant.yaml  # scoped danger-zone authorization (docs/trust-model.md)
agent spawn 123 -m "..."       # ad-hoc: an interactive agent in the worktree, wired to report back
agent report 123 --state done  # ...what that agent (or its stop hook) reports on the way out
agent review 45                # read-only review of PR #45 (add --post to comment)
agent review 45 --surface orca # same, but on a visible Orca terminal instead of inline
agent review --all             # review every open PR targeting base_branch, concurrently
agent merge 45                 # squash-merge PR #45 into staging if merge rules pass
agent promote                  # open the staging → main promotion PR for human verification
agent worktree list            # see in-flight task worktrees
agent runs                     # per-issue state: running / halted / stopped / done
agent runs --wait              # block until every tracked run finishes, printing transitions
agent status --failures        # recent failed workflow runs across every registered repo
agent status --sync-orca       # mirror active agent lanes onto Orca cards (read-only)
agent runtimes                 # claude_code / codex availability
```

The pipeline TUI opens in the Catppuccin Macchiato theme by default; set
`tui.theme` in `.agent/config.yaml` to any of Textual's built-in themes (also
switchable live via its command palette, `ctrl+p` → "theme") — an unknown
name fails at startup with the valid list.

Stages fan out across roles via model tiers: **planner** and **reviewer**
run the `smart` tier (currently `fable`) in read-only mode; **implementer**
runs the `fast` tier (currently `sonnet`) with write access. A tier names a
job, not a vendor, and each runtime maps the tiers to its own models under
`model_tiers:` in `config/defaults.yaml`, so swapping the fleet is one edit;
override per project or per role under `agents:` in `.agent/config.yaml`.
A tier the effective runtime does not define is a named error rather than a
foreign model name handed to a CLI — `agent doctor` lists what each runtime
resolves to and flags the gaps.
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

## CI lane (scheduled pipelines)

Each managed repo gets stub workflows (`stubs/managed-repo-*.yml`) calling
the reusable pipelines here. Six pipelines ship today, but only five have a
caller workflow with its own `name:`/`cron:` in this repo's
`.github/workflows/` — promote doesn't; only the reusable
`promote-pipeline.yml` lives here, dispatch-only:

| Pipeline | Workflow | Cadence | Runs |
| --- | --- | --- | --- |
| Triage | `triage.yml` | every 4 hours | orchestrator prompt (all 4 roles) |
| Groom | `groom.yml` | daily, 01:00 UTC | `agent groom` CLI |
| Scout | `scout.yml` | daily, 18:00 UTC | `agent scout` CLI |
| Spec | `spec.yml` | nightly, 19:00 UTC (picks up issues labeled `spec-requested`) | `agent spec` CLI |
| Plan | `plan.yml` | nightly, 19:20 UTC (picks up issues labeled `plan-requested`) | `agent plan --post` CLI |
| Promote | `promote-pipeline.yml` | dispatch-only here; the stub adds a daily 01:30 UTC cron once copied to a managed repo | `agent promote` CLI |

Promote is the odd one out: every other lane has a caller workflow with a
cron here, and promote only has the reusable pipeline. Its stub carries a
cron regardless, so a managed repo gets a scheduled promotion PR that this
repo itself never runs on a timer.

All six call a `*-pipeline.yml` reusable workflow, but they don't all run
the same thing: only triage runs the orchestrator prompt
(`prompts/orchestrator.md`, Planner → Implementer → Tester → Reviewer with
fresh context per agent). Groom, scout, spec, plan, and promote each run the
matching `agent <verb>` CLI directly instead — the same code path as the
local lane, so output can't drift between them. Branch model per managed
repo:

    fix/issue-N ──► staging (agent auto-merge, gated) ──► main (human only)
    hotfix/issue-N ──► main (human merge) ──► back-merge to staging

Setup:

1. `bash setup.sh` (git init, create GitHub repo)
2. `claude setup-token` → add as `CLAUDE_CODE_OAUTH_TOKEN` secret (org-level
   if managing multiple repos)
3. Per managed repo: create `staging`, labels, branch protection, copy the
   stub workflow (setup.sh prints the exact commands)
4. Leave `auto_merge: false` in the stub workflow (report-only) for the
   first week; merge caps and blocked paths come from the target repo's
   `.agent/config.yaml`, not `config/repos.yml`

### Safety gates (enforced in prompt AND GitHub settings)

- Branch protection: `main` requires human-approved PR; `staging` requires
  green checks
- Auto-merge only if: tests PASS, review APPROVE, CI green, no blocked label,
  and `agent merge --check` reports no violations. Both lanes ask the same
  code (`evaluate_merge`) since #150, so the caps mean the same thing in
  either — they live in `merge.max_changed_lines` / `merge.max_changed_files`,
  and `merge.blocked_paths` still covers CI/auth/migrations/deps/infra. The
  orchestrator keeps one open-ended prose rule on top, for infra files the
  coded list doesn't enumerate. See `docs/adr/0005-one-merge-cap-evaluator.md`.
- Hotfixes are never auto-merged; one revision round per stage, then escalate
- Caps vary per pipeline: triage caps at `max_issues: 3` (see `triage.yml`);
  scout defaults to 3, spec and plan default to 2 (each overridable via
  workflow input); groom has no per-run cap beyond the 100 open issues it
  fetches. Every pipeline sets its own `timeout-minutes`; triage, groom,
  scout, spec, and plan additionally share one concurrency group per repo
  so they never race on the same labels — promote isn't in that group

### Operating it

- **Pause one repo:** disable each of its caller workflows (Actions → ⋯ →
  Disable, once per pipeline) — disabling triage alone leaves groom, scout,
  spec, plan and promote running on their own crons, since every stub ships
  with one (see the cadence table above)
- **Run manually:** Actions → the triage workflow (`triage.yml`, every 4
  hours) → Run workflow
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
config/defaults.yaml platform defaults; config/repos.yml CI-lane defaults
                     (the managed-repo registry is config/local/repos.yml,
                     git-ignored, so no private repo names live in this
                     public repo)
stubs/               workflow stubs to copy into managed repos
docs/                architecture, ci-cd, workflow, guide, failure-modes,
                      trust-model, roadmap, adr/, office-ops suggestion
docs/reference/lanes.md   which of the 8 lanes share one implementation
                      between local and CI, and which still diverge
```

## Notes on subscription usage

Local runs and CI runs draw from the same subscription quota as interactive
Claude Code sessions. Subscriptions are intended for single users — for
multi-user or heavy unattended automation, switch the CI lane to API-key
billing. Regenerate with `claude setup-token` if CI runs start failing auth.
