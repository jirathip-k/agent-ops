# Task: distill AGENTS.md

You are pruning this repository's `AGENTS.md` — a distillation pass, not an
editing pass. Durable knowledge should survive; run-by-run narration that
already paid for itself should go.

## What you may touch

The following sections are human-authored and off-limits. Do not add,
remove, or reword a single character inside them, and do not add a new
heading with one of these names:

{protected_sections}

Every other section was appended by an earlier agent run. These are the
only sections you may edit or remove:

{prunable_sections}

## When to cut

Glance at size and shape first. A lean file, or a prunable section that is
already tight, is a no-op — say so and stop. Distill for real only when a
section has swollen with dated, run-specific detail.

## What has no future value

- Run-by-run narration ("tests green", "PR #93 opened", tool-by-tool
  play-by-play)
- Resolved one-off incidents
- Superseded plans and abandoned hypotheses — state the conclusion a later
  run needs, not the path taken to reach it
- Duplication where the same finding already appears elsewhere in the file

## What is durable — always keep

- Settled baselines and stable identifiers a run would otherwise re-derive
- Gotchas that recur
- Owner preferences and working conventions

If a durable finding is buried in narration, fold it into the right
existing heading among the prunable sections instead of leaving it where it
was. Moving content between prunable sections is fine; moving it into a
protected section is not — and neither is moving it into a new file. This
pass deletes spent text; it does not archive it. Do not create, rename, or
write to any file other than `AGENTS.md`.

## Safety rails

Distillation is subtractive, so:

- Never touch a protected section listed above.
- Never drop an open item — a live TODO, an unresolved blocker, an active
  goal.
- Never drop a value something else depends on.
- **When uncertain, keep.** A wrongly-kept line is mild clutter; a
  wrongly-cut learning is lost memory the loop paid runs to acquire.

## Output format

End your final message with a block in exactly this form (nothing after
it):

DISTILL REPORT:
<section> — <what was cut> — <why>

One line per cut, naming the section it came from. If nothing needed
pruning, output exactly:

DISTILL REPORT:
none
