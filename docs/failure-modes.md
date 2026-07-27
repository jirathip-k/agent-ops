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

Tests and review are not who finds out. Every mode in the spawn lane below
shipped through both and none survived one real use: a path that only runs
against a live terminal, a real approval prompt or a second process is
unexercised code however green the suite is. The fixes were held to the
opposite standard — each guard below was watched failing on a reverted fix,
then watched working in a live `agent spawn` session.

## CI lane

| Failure | Why it was invisible | What catches it now |
|---|---|---|
| Pipeline pushes attributed to `github-actions[bot]`, whose workflow runs GitHub holds at `action_required` | The auto-merge gate saw an empty check rollup and read it as "CI not green", so PRs were held with no stated reason | App identity for pushes (#59); the gate distinguishes "CI failed" from "CI never ran" (#56) |
| A reusable workflow cannot read the caller's secrets | Secrets were set on the repo and *looked* configured; inside the pipeline they were empty, so the mint step skipped and checkout silently fell back to `GITHUB_TOKEN` | Callers forward them explicitly; `agent doctor` reports a caller missing keys the stub declares (#61) |
| Managed repos run a stale copy of a `stubs/managed-repo-*.yml` caller | `agent init` writes the stub once at onboarding; copies never follow the source | `agent doctor` structural drift check (#61), run for every lane the repo calls (#90) |
| A spec that opened with the word `ESCALATE` on the way to ruling it out | The bare `startswith("ESCALATE")` read it as an escalation and discarded a complete spec; the gate label only clears on success, so the run repeated nightly and failed identically, indistinguishable from a real failure in `agent status --failures` | The sentinel needs a boundary after it — end of line, the documented colon, or other punctuation (#128/#130 required the colon; #129 widened that to a boundary so the fix for a loud failure could not introduce a silent one). An escalation now posts its reasoning on the issue, and a near miss — the word, then prose — is logged rather than passed in silence (#129) |
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
| A stalled streaming run (wedged tool call, dead API connection) never exited | The pid stayed real and `agent runs` still reported `running`; the worktree stayed claimed and a re-dispatch would have reused a checkout a live process still owned | `_run_streaming` gets an idle timeout (silence, not total duration); the non-streaming `run()` path gets a wall-clock bound since it has no stream to watch for silence — both surface as a normal failed `RunResult` (#108) |
| A halted run whose worktree was cleaned up had no recovery path | `agent resume <N>` saw a missing `.worktrees/issue-N` and told the operator to run `agent dispatch N` — two of three liveness signals (`.agent-runs/` feedback, an open PR) said the run was alive, but `resume` treated it as never started. Following the advice was destructive: `dispatch` builds a fresh worktree from `base_branch`, never checking out `fix/issue-N`, and its open-PR guard's only escape hatch (`--force`) starts a competing duplicate implementation | `resume` reconstitutes the worktree from the surviving branch — local, remote, or both — and names it in the log; a strictly-behind local branch is fast-forwarded (`merge --ff-only`, never `reset --hard`), and true divergence (commits on both sides) is refused rather than guessed at (#164) |
| A worktree cut from `main` in the morning self-reviewed a tree six merges behind by lunchtime | Missing merged work reads as a missing feature and already-merged work reads as new code — both produce a confident, well-argued, entirely wrong review. One instance's fourth finding was a false safety objection telling the branch not to merge, on the strength of a warning that sounded like diligence | `_refuse_if_stale_base` compares `HEAD` against `origin/<base_branch>` before self-review or a PR and refuses, naming how far behind and the remediation, rather than reviewing or landing a stale tree (#184) |

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

`agent runs` assembles these into one command (#78). Building it took three
self-review rounds, each finding a way the liveness signal lied:

| Round | Failure | Why it was invisible |
|---|---|---|
| 1 | Matched a process named `agent`, which never exists | `agent` is a console-script entry point, so the shebang loader rewrites argv — a live run is `python3 …/agent implement <N>`. Every running dispatch would have reported `stopped` |
| 2 | Matched machine-wide, on issue number alone | Issue numbers are small and collide across managed repos, so a live `agent implement 68 --project ~/Projects/sendmeter` would be reported as *this* repo's #68, with a foreign pid. The disambiguator was already in the argv: `--project` |
| 3 | The `--project` filter dropped *true* positives | `args.split()` breaks a path containing a space into several tokens, so the declared root never matched and a live run reported `stopped`. An unreadable value means "unknown", not "elsewhere" |
| 3 | A healthy in-flight `dispatch` reported `stopped`, advising "re-dispatch" | `dispatch` creates the worktree, then retries the Orca attach for ~4s before the child execs. Following that advice re-enters `worktree.create(reuse=True)`, which accepts the pristine checkout — putting **a second agent in the same worktree** |
| 3 | The `ps` call itself had no test | Every test stubbed it. A flag or column-order change fails silently: liveness comes back empty and every run reports `stopped` |

The pattern holds even here: each of these produced a confident, plausible
answer that was wrong, rather than an error.

Every row of that table is a *proxy*, which is why the same bug kept coming
back wearing different clothes (#86, #87, #88, #92). A run that finished had no
way to simply say so. It does now: where Orca is present, a run pushes its own
terminal state over the orchestration bus and `agent runs --wait` wakes on it
(#98). Two things keep that from becoming a fifth thing that can lie:

- It is **only ever a shortcut.** A report can end a wait early; its absence
  can never extend one. Drop every message and the derivation above runs on
  exactly the cadence it always did, so no Orca — the background surface, the
  CI lane, a forgotten terminal handle — costs latency, not correctness.
- It is **not a record.** Messages are consumed once and are never the only
  place a fact lives; the durable answers stay in GitHub and in the outcome
  record (#87). That is also why the one-shot `agent runs` snapshot does not
  read them — draining a message to render a snapshot would lose it.

The one state it adds rather than hurries is `failed`: a run whose gates never
passed leaves a worktree, no PR and no feedback, which is indistinguishable
from an abandoned one. No table row can recover that — only the run itself
knows.

## Spawn lane

`agent spawn` shipped in #114 with 419 lines of passing tests, green CI and a
careful review. Every mode below was found within five minutes of its first
real use. That gap between "verified" and "exercised" is the entry, as much as
any one row.

| Failure | Why it was invisible | What catches it now |
|---|---|---|
| A spawned interactive session runs with no permission mode | `build_interactive_command` never passed `--permission-mode`, which the headless `build_command` always does. The worker stopped for approval on every edit, and delegated work waiting on a human who may not be watching looks exactly like delegated work in progress | `interactive_command` takes a mode, defaulted high and validated before the worktree is created (#115/#118) |
| A live spawn reads as `stopped`, and the wait the tool recommends confirms it | `_RUN_VERBS` omitted spawn and could not have helped anyway — the process on the surface is `claude`, not `agent` — and `classify` never consulted the spawn record, so a working agent got "worktree kept, no PR, no feedback — inspect". `agent spawn` then printed "wait with `agent runs <N> --wait`", and since #86/#89 two consecutive `stopped` polls are terminal: the recommended wait returned success after ~30s. The same false-`stopped` class as #86, back through a path the classifier did not know existed | `runs.spawn_state` asks the surface — `orca terminal show` for a handle, `os.kill(pid, 0)` for the background surface — and `classify` reports `running`, which is not in `TERMINAL_STATES`. An Orca that cannot answer degrades the poll's `trustworthy` flag instead of producing a verdict, so an outage costs latency rather than a false finish (#116) |
| A transient attach timeout orphans a live terminal | `_attempt_orca_attach` retried only `selector_not_found` and raised on anything else. A real `agent spawn 108` raised `Timed out waiting for terminal handle after creation` — *after* the terminal had been created — and the retry made a second one. Verified: two live sessions on one worktree and one branch, with the spawn record naming only one handle, so the other was invisible to `agent runs` and unaddressable by `messages.send_outcome`. Shared with `dispatch`, not unique to spawn | `orca.is_transient` widens the retry past `selector_not_found`; each failed attempt then diffs the worktree's terminals against a baseline and *adopts* one that appeared rather than adding another. `run_spawn` refuses outright to spawn onto a worktree that already hosts a session, and an attach that ultimately fails names the worktree it kept and how to retry or drop it (#117) |
| A healthy agent is recorded `halted` a minute into its run | The hook was seeded on `Stop`, on the reading that a turn ends because the agent is stuck. It does not — a turn ends whenever the assistant finishes speaking. With `--if-unreported` binding the one report slot to the first caller, the earliest and least informative verdict was guaranteed to be the one that stuck, and it then became the run's answer for its whole life | The hook fires at `SessionEnd` only, and its reason claims just that. What it gives up is knowingly given up: a worker that finishes, says nothing and leaves the session open now reads `running` until the session closes, which is true, rather than `halted`, which was not (#120) |
| A run's completion report is delivered to the run itself | `send_outcome` put the same handle on `--to` and `--from`, and it came from the spawn record — which stored the *worker's* terminal. `SpawnRecord` had no field for who asked for the work. So the worker was interrupted by its own report and had to spend a turn dismissing it, while the party waiting on the work was told nothing and could only poll, which it could already do | `SpawnRecord.spawner`, captured from `ORCA_TERMINAL_HANDLE` at spawn time — the only moment that identity exists. `collect`/`wait_for_message` read the same mailbox `send_outcome` writes to, so addressing the spawner does not quietly break `--wait`. No spawner (a run started by hand) falls back to today's pollable per-run mailbox (#122) |
| A resume silently dropped the spawner every cycle after the first | `dispatch_resume` calls the same `record_spawn` that scopes the mailbox to the fresh terminal (correct — issue #98), but called it without `spawner=`, which defaults to `None`. Since `record_spawn` overwrites the whole record, each resume erased the link `dispatch` had recorded. Halts reached the supervisor for round 1, then landed in a per-run mailbox nobody was watching for every round after — indistinguishable from a run still in progress | `dispatch_resume` threads the spawner through: `current_handle()` (whoever ran `agent resume`) when set, else the prior record's spawner (a resume from a plain shell), guarded so the worker is never recorded as its own spawner (#192) |
| `agent report --pr` accepted anything | Documented as "URL of the PR this run opened" and typed `str`. `--pr 121` stored `{"pr_url": "121"}`, and `agent runs` — which parses the number off the end of a URL — rendered `PR #121 — 121`, next to correctly-rendered rows from the same run | `github.normalise_pr_url` at the CLI boundary: a bare number is expanded offline from the `origin` remote, anything else that is not a `…/pull/<n>` URL is refused with nothing recorded (#122) |

### What they did together

The three original modes composed into a fourth, worse than any of them. The
orphaned spawn left two agents on one branch; both carried the per-worktree
stop hook, and `--if-unreported` gives whichever exits first the single report
slot. The orphan noticed the other, stood down, and took it:

> halted — "Duplicate dispatch: two agent processes (pid 81644 and 82661) …
> racing edits into runtimes/base.py"

So `agent runs 108` showed `halted`, needs-human, while the real agent was
seven minutes into the task and working. A supervisor reading that verdict
would have concluded the opposite of the truth, twice over — the run that
reported was the one that should not have existed.

The part worth keeping is not the misreport. Two uncoordinated agents shared a
branch, and the only thing standing between them and interleaved edits was one
agent noticing the other and choosing to stand down. Nothing in the platform
arranged that, detected it, or would have stopped it. It was luck, and the fix
is not that the report is now correct — it is that the retry adopts instead of
duplicating, and that a spawn onto an occupied worktree is refused before the
worktree is touched. The second session never starts.

### What is still only a proxy here

Terminal liveness answers "does this session exist", not "is the agent inside
it still working" — Orca keeps the pane open after the command in it exits. The
outcome record is what closes that gap, and it is written by a hook. So a
runtime with no hook mechanism (Codex today) reads `running` for as long as its
terminal is open, and `agent spawn` says so at spawn time rather than letting
the state imply otherwise. Killing the process outright (SIGKILL, power loss)
is the same silence it has always been.

## Cross-lane coordination

The two dispatch lanes shared no signal, so on 2026-07-26 both implemented #116
at once: a hand-dispatched local agent (#126) and the scheduled CI triage lane
(#127), five minutes apart. Two paid sessions, one outcome, noticed only because
a human read the PR list.

| Failure | Why it was invisible | What catches it now |
|---|---|---|
| Two lanes implement the same issue in parallel | Every label the CI lane selects on describes an issue's *classification* — `triage:done`, `needs-human`, `blocked`. None means "an agent is working on this right now". The local side's evidence (a worktree, `.agent-runs/`) lives on one machine; a fresh CI runner cannot read any of it, so an issue under active local work looks exactly like an untouched one | An `agent:claimed` label, applied by `agent implement`/`resume`/`spawn` and skipped by the CI lane's Step 1 selector (#131). Released in a `finally` around the whole run, so no exit path can forget |
| Merging the narrower of two duplicate PRs *masks* a bug rather than fixing it | Both PRs are green and well-tested, so the choice looks like a preference. Fixing #116 alone hid #120: `classify` ranks `live > outcome`, so a detected-live spawn hides a false `halted` record that still becomes the run's verdict once the session ends | The duplication itself, upstream — there is no second PR to choose between (#131) |

### The one it introduces

A claim mechanism can strand an issue, and that is a worse trade than the
duplication it prevents: duplicate work is loud and costs money, a blocked issue
is silent and costs nothing anyone can see. Written down as a failure mode
rather than as a caveat, because it is one.

**A claim can outlive its run.** The release is a `gh` call at the end of a
process, so anything that skips the end skips the release: SIGKILL, power loss, a
`gh`/network outage in the last second of a run, or a spawned session under a
runtime with no stop hook (Codex today — the same gap `agent spawn` already has
for outcome records). The issue then carries `agent:claimed` with nothing behind
it, and *no lane will touch it* until something clears it. Three recoveries, in
decreasing order of how much they assume:

- the run releases it (`finally`, or the session-end hook via `agent report`);
- the CI lane treats a claim older than 8h as dead, clears it with a comment, and
  proceeds — enforced by the *reader*, deliberately, because a claim left by a
  laptop that is now closed must expire with nothing local ever running again;
- `agent doctor` names it, with the command to clear it, long before the TTL.

The eight hours between a crash and the TTL are real blocked time, and nothing
shortens them for an operator who never runs `doctor`. That is the cost, stated
rather than discounted.

**A hand-started agent now claims itself, if it can.** `agent implement`, `agent
resume` and `agent spawn` claim on their own; an agent started by hand in a
worktree — how most of this repo's own work happens, including #126 and later
sendmeter #269 — runs no agent-ops command at all, so nothing used to claim on
its behalf. `agent init` now seeds a checked-in `SessionStart` hook that runs
`agent claim --auto`, deriving the issue from the `fix/issue-N` branch, so the
claim is a consequence of the session starting rather than a second command
somebody has to remember (ADR 0006). It degrades exactly to the old behavior
whenever it can't run: a declined hook, a non-Claude runtime, a repo that hasn't
re-run `init`, or a branch the convention doesn't match. `agent claim <N>` by
hand, and the inverse check — `agent doctor` reporting a `fix/issue-N` worktree
on this machine whose issue carries no claim — are what's left for those cases.
That report is still just a report, not a guard: the collision it describes
remains possible wherever the hook doesn't run.

**`agent doctor` cannot tell a foreign claim from a dead one.** A claim held by
another machine looks identical to one whose run died: no local worktree, no
local outcome record. Reporting that as stale would produce confident, wrong
advice — the exact shape every entry above shares — so it is not reported at all.
Only two things count as stale: age past the TTL, and a local outcome record
written *after* the claim was applied (a release that failed). Everything else is
left to the TTL, which is slower and correct.

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

The same applies to the code under test: the one unstubbed thing in `agent
runs` — the `ps` invocation — was the one thing no test touched. Stubbing the
boundary is right for logic tests, but something must still exercise the real
call, or the feature's central claim rests on nothing.
