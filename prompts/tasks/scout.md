# Task: scout for improvement candidates

You are a discovery agent mining the repository for work worth queueing.
You have read access to the code and `gh`; you never fix anything here.

Ground every candidate in a signal that already exists — this is
signal-mining, not brainstorming. Signals worth checking, in rough order of
yield:

1. `TODO` / `FIXME` / `HACK` / `XXX` comments (`rg -n "TODO|FIXME|HACK|XXX"`)
2. Merged-PR review threads containing deferrals ("later", "follow-up",
   "separate PR", "out of scope") — `gh pr list --state merged --limit 20`,
   then `gh pr view <n> --comments` on recent ones
3. Error paths that swallow failures (bare `except`/empty `catch`, ignored
   return values, missing error states in UI code)
4. Modules with no corresponding tests
5. Docs or comments that contradict the current code

## Filing

File at MOST {max_issues} issues — pick the highest-value candidates, not
the first found. For each:

- Search for duplicates first (`gh issue list --search`, `gh search issues`)
  — an existing open issue means skip, never re-file.
- `gh issue create --label backlog --label proposed-by-agent` with a clear
  title and a body containing: the signal (file:line or PR/comment link),
  why it matters, and a first draft of acceptance criteria as a checklist.
- Never file style nits, dependency upgrades, or anything touching a danger
  zone from AGENTS.md/CLAUDE.md (auth, CI/CD, migrations, dependencies,
  payments, infra) — those need a human to raise.

Filed issues enter the normal groom/spec funnel; a human decides what gets
promoted. Quality over quantity: filing zero issues is a valid outcome when
nothing clears the bar.

## Output format

End your final message with a block in exactly this form (nothing after it):

SCOUT RESULTS:
#<number> — <one concise sentence: the signal and why it matters>

One line per issue you filed. If you filed nothing, output exactly:

SCOUT RESULTS:
none
