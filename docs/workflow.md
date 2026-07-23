# Day-to-day workflow

How work actually flows through the platform: capture → groom → dispatch →
review → merge, with GitHub Projects as the board and Herdr as the cockpit
for parallel runs.

## 0. One-time per project

```sh
cd ~/Projects/your-app
agent init        # AGENTS.md (canonical) + CLAUDE.md symlink + .agent/config.yaml
agent doctor
```

`AGENTS.md` is the canonical instruction file; `CLAUDE.md` is a symlink to it
so Claude Code, Codex, and any other runtime read the same project knowledge.
If a repo already has a real `CLAUDE.md`, `init` leaves it alone — either keep
both in sync manually or replace one with a symlink yourself.

## 1. Capture — everything becomes a GitHub issue

Ideas, bugs, chores — file them where you are (`gh issue create`, GitHub
mobile, a Claude session). An issue is the unit of agent work; if it isn't an
issue, the agents can't see it.

## 2. Groom — decide what an agent may do

An issue is agent-ready when it has acceptance criteria, is small enough to
verify (roughly ≤ half a day of human work), and touches no danger zone from
`AGENTS.md`. Then:

```sh
gh issue edit 123 --add-label agent-ready
```

The label is the contract. The CI lane's stricter equivalent is
`approved-for-agent` (see `prompts/orchestrator.md`); locally you're the
gate, so one label is enough.

## 3. Dispatch — run the loop

```sh
agent queue                 # open agent-ready issues, oldest first
agent implement 123         # worktree → loop → gates → self-review → PR
```

Parallelism is free because every task gets its own worktree — run several
`agent implement` commands at once (see Herdr below). While building trust in
a new project, use `--no-pr` and inspect the kept worktree before pushing.

## 4. Review & merge — humans own main

```sh
agent review 45 --post      # agent pre-review as a PR comment
gh pr checkout 45           # your own look
gh pr merge 45 --squash
```

The agent review is a pre-filter, never the approval. Merge is always yours.

## 5. Background — the CI lane

The scheduled triage pipeline handles the long tail (triage, small fixes,
audit issues) across repos registered in `config/repos.yml`. Check its run
summaries and `needs-human` labels once a day; that's your ops inbox.

## GitHub Projects (the board)

Use one user-level Projects v2 board across all your repos as the human view;
agents never read the board — they key off labels, which are visible in
`gh issue view` and survive in the issue itself.

Setup (once, in the GitHub UI):

1. Create a user Project "Dev board" with a Status field:
   `Backlog → Ready (agent) → In progress → In review → Done`.
2. Enable the built-in workflows *Item closed → Done* and
   *Pull request merged → Done*.
3. Feed issues in from every repo. The built-in **Auto-add** workflow is
   limited to ONE per project on the Free plan (it can watch only one repo),
   so use it for your busiest repo and copy
   `stubs/managed-repo-project-sync.yml` into each additional repo — an
   `actions/add-to-project` workflow that adds `agent-ready` issues to the
   board with no repo limit (needs a classic PAT with `project` scope; see
   the stub's header). One-offs: `gh project item-add <number> --owner @me
   --url <issue-url>`.
4. Optional CLI access needs an extra scope: `gh auth refresh -s project`,
   then `gh project item-list <number> --owner @me`.

Convention: moving a card to **Ready (agent)** means you add the
`agent-ready` label (Projects workflows can't add labels; the label is the
source of truth, the column mirrors it). PRs opened by `agent implement`
carry `Closes #N`, so merge closes the issue and the board sweeps itself.

## Herdr (running agents in parallel)

[Herdr](https://github.com/ogulcancelik/herdr) is a terminal multiplexer
built for AI coding agents: it detects Claude Code/Codex processes in its panes, shows
which are working / blocked / finished, and keeps sessions alive when you
close the window. It pairs naturally with worktree-per-task:

- **One workspace per project**, one pane per task.
- **Pane 1**: interactive `claude` in the main checkout — grooming issues,
  exploring, writing acceptance criteria.
- **Panes 2+**: either `agent implement <N>` (headless, fire-and-check-back)
  or an interactive `claude` inside `.worktrees/issue-<N>` for tasks that
  need supervision. Worktrees guarantee the panes never trample each other.
- `agent implement` streams the underlying agent's activity live — every
  tool call (`⚙ Bash: uv run pytest -q`) and thought line — interleaved with
  the stage log (planning → attempts → gates → verdict), so a pane always
  shows what the agent is actually doing. Set `runtime.stream: false` in
  config for quiet output.
- Herdr's blocked-state detection is your signal to jump into a pane that's
  waiting on a permission prompt.
- `agent worktree list` reconciles what's actually in flight if you lose
  track of panes.

Herdr replaces scattered terminal windows; it doesn't replace the platform's
gates — a run only becomes a PR when tests, lint, and self-review pass,
regardless of which pane it ran in.
