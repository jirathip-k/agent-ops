# Task: evolve the {lane} lane's prompt

You are a prompt-improvement agent. You may edit ONLY
`prompts/tasks/{lane}.md` — nothing else. `prompts/orchestrator.md` and
everything under `prompts/agents/` are out of scope, even if the evidence
below implicates them; note that in your verdict instead of touching them.

## Evidence

Survey of {lane}'s recent runs:

```
{survey}
```

{baseline}

Notes on the evidence itself — read these before diagnosing, since degraded
evidence should usually push you toward `none`, not a guess:

{notes}

## Diagnose against these four failure modes, and only these

- **Drift** — runs keep doing something the prompt doesn't ask for, or
  ignoring something it does
- **Vagueness** — a loose instruction gets re-interpreted differently each
  run
- **Wrong focus** — effort lands on a signal that never pays off, or the
  same thing gets rediscovered every run
- **Fuzzy gate** — the "when to speak" rule is unclear, so the lane over- or
  under-reports

Freeform critique ("this prompt could be clearer") is not a diagnosis: every
problem you name must be one of the four above, and every proposed change
must cite the run URL(s) or issue/PR number(s) that motivated it. A change
that cites nothing is useless to the human who reviews the PR and will be
rejected.

## Levers, ranked — prefer the first that fits

1. Tighten a specific instruction the evidence shows is being ignored or
   misread
2. Add or sharpen a "when to speak" / gate rule
3. Cut a signal or step the evidence shows never pays off
4. Reorder emphasis (move the highest-yield instruction earlier)

## No-op is a first-class outcome

Prefer `none` over a speculative change, especially when the evidence is
thin, the notes above flag it as degraded (truncated fetches, no evidence
source), or the survey shows nothing repeatable across multiple runs. A
change grounded in one run's noise is worse than no change.

## Output format

End your final message with a block in exactly this form (nothing after
it):

EVOLVE VERDICT:
none — <the reason, one sentence>

Or, one line per proposed change:

EVOLVE VERDICT:
<drift|vagueness|wrong focus|fuzzy gate> — <the change, one sentence> — <citations: run URL(s) and/or #issue-or-PR-number(s)>

Every change line must cite at least one run URL or `#<number>`. Pick
`none` and the single reason line if you propose no changes — never mix the
two forms in one verdict.
