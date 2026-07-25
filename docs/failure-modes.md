# Silent failure modes

A catalogue of ways the agent lanes have failed *quietly* — producing a
plausible-looking result while doing the wrong thing, or doing nothing at all.
Loud failures fix themselves; these are the ones that cost debugging cycles.

Each entry records what made it invisible and what catches it now. Read this
before adding a code path that can degrade.

## The recurring shape

Every entry below is the same story: **a fallback that didn't say it fell
back.** The system stayed up, produced output, and looked healthy — while
skipping the thing it was there to do. When adding a degradation path, the
question is not "does it recover?" but "does anyone find out?"

## CI lane

| Failure | Why it was invisible | What catches it now |
|---|---|---|
| Pipeline pushes attributed to `github-actions[bot]`, whose workflow runs GitHub holds at `action_required` | The auto-merge gate saw an empty check rollup and read it as "CI not green", so PRs were held with no stated reason | App identity for pushes (#59); the gate distinguishes "CI failed" from "CI never ran" (#56) |
| A reusable workflow cannot read the caller's secrets | Secrets were set on the repo and *looked* configured; inside the pipeline they were empty, so the mint step skipped and checkout silently fell back to `GITHUB_TOKEN` | Callers forward them explicitly; `agent doctor` reports a caller missing keys the stub declares (#61) |
| Managed repos run a stale copy of `stubs/managed-repo-triage.yml` | `agent init` writes the stub once at onboarding; copies never follow the source | `agent doctor` structural drift check (#61) |
| A missing or mis-scoped App installation would fail the mint step and take the whole triage job down | The step became load-bearing the moment secrets existed — most likely mid-rollout | `continue-on-error` plus an explicit `::warning::` naming the repo (#62) |

## Local lane

| Failure | Why it was invisible | What catches it now |
|---|---|---|
| A run halted at self-review kept its worktree and dropped the findings | No PR, no comment, no log the caller could see — indistinguishable from an unstarted issue | The halt stashes findings under `.agent-runs/`, comments on the issue, and `agent resume <N>` continues it (#73) |
| The planner never saw issue comments | An approved `## Agent spec` was posted and simply not read; the plan looked like the planner had disagreed | The planner renders the issue thread, pinning spec/plan comments past the recency cutoff (#52) |
| Self-review never saw untracked files | `git diff` ignores them, so a create-only run — the common shape for "add X" — was reported as an empty diff and skipped review entirely | An intent-to-add pass before the diff (#75) |
| An empty diff was reported as REQUEST CHANGES | Posted "changes requested — (empty diff — nothing to review)" on the issue and stored it as the next run's feedback | `SelfReview.reviewed` distinguishes "nothing to review" from a rejection (#75) |
| The dedupe guard matched any `#N` in a PR body | A PR that merely cross-referenced an issue blocked implementing it, and the message read as though the issue were already handled | Matches GitHub's own `closingIssuesReferences` (#64) |
| Orca card status was dropped when a worktree card wasn't indexed yet | The run was visible in a terminal on a card whose status never changed | `orca.report` follows the surface's fallback, sticky once used (#68) |
| The editable install ran a stale working tree | `agent dispatch --help` correctly showed no `--plan-file`; the tree was four commits behind, so the CLI was right about itself and wrong about the world | `agent doctor` reports the checkout behind its upstream (#67) |
| Ad-hoc `--message` overwrote the stored halt findings | The self-review was lost permanently; a later bare `agent resume` replayed the one-line note instead | Ad-hoc messages stage to their own path (#75) |

## Answering "is this run finished?"

Terminal output is not the answer. A terminal that has exited returns an empty
buffer, and one hosting a TUI returns chrome rather than scrollback — so a run
that died thirty seconds in looks the same as one still working.

The durable signals, all derivable:

| State | Worktree | Process | Feedback file | PR |
|---|---|---|---|---|
| running | present | alive | — | — |
| halted at self-review | present | gone | present | — |
| stopped (died) | present | gone | absent | — |
| done | removed | gone | absent | open |

`agent runs` assembles these into one command (#78). Two traps it had to
survive: `agent` is a console-script entry point, so a live run appears in `ps`
as `python3 …/agent implement <N>`, never `agent`; and issue numbers collide
across managed repos, so liveness must be scoped by the `--project` argument
the dispatched argv already carries.

## Tests

A passing test is not evidence that a test guards anything. Two cases from one
day:

- The intent-to-add fix — the line making create-only runs reviewable at all —
  could be deleted with all 319 tests still green.
- A test named `..._does_not_overwrite_the_halt_findings` asserted only that two
  path helpers return different strings. It never called the code, so the
  regression it was named for would not have failed it.

For a fix whose whole value is preventing a silent regression, revert the fix
and watch the test fail before trusting it.
