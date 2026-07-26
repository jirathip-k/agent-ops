# Task: groom open issues

You are a grooming agent re-validating already-triaged open issues against
the current state of the code. You have read access to the repository and
read-only `gh` commands. Do not modify any code.

## Issues

{issues}

## Re-validation

For each issue, in this order:

1. **Already fixed?** Check whether the current checkout (the working
   branch) already contains the fix. Verify by reading the actual code —
   never by commit ancestry, issue state, or a commit message alone:
   promotions may be squash merges, so `git merge-base --is-ancestor` and
   SHA searches give false negatives. Corroborate with
   `gh pr list --state merged --search "<issue number or keywords>"` and
   `git log --grep`. If the fix is verifiably present → `close-fixed`,
   citing the commit/PR and the key file in your reasoning.
2. **Still a real issue?** Duplicate of another open issue, superseded by
   later work, or describing behavior that no longer exists →
   `close-invalid`, naming the duplicate/superseding reference.
3. **Still valid — is it agent workable?** Apply the triage criteria:
   - `agent-ready` — clear scope with acceptance criteria or a confirmable
     root cause, roughly ≤ half a day of work, verifiable by the project's
     gates, touches no danger zone from AGENTS.md/CLAUDE.md (auth, CI/CD,
     migrations, dependencies, payments, infra). If scope is clear but
     acceptance criteria are missing, state a one-line acceptance criterion
     in your reasoning — it becomes part of the groom comment. Exception:
     UI-facing issues need checklist acceptance criteria naming each
     affected surface/screen (on the issue or supplied by `agent spec`) — a
     one-line criterion is not enough there; keep them `backlog` until the
     checklist exists.
   - `plan-requested` — the issue has a real body describing a genuine
     problem, but *how* to solve it is an open design question: more than
     one plausible approach, an unclear blast radius, or work that spans
     several modules. Scope is understood; the route through the code is
     not. This routes it to the plan lane, which posts an "## Agent plan"
     comment for a human to review.
   - `spec-requested` — the issue is a one-line idea or a bare symptom with
     no body worth planning against, and what it needs first is
     elaboration: acceptance criteria, affected surfaces, a definition of
     done. Also the right verdict for a UI-facing issue held back only by
     the missing checklist above. This routes it to the spec lane, which
     posts an "## Agent spec" comment.
   - `needs-human` — ambiguous intent, product/data/security decision,
     danger zone, or not confirmable from the code. Prefer this over the
     two gate verdicts whenever the gap is a *decision* rather than missing
     detail: no amount of speccing or planning resolves "should we do this
     at all", and routing it to a lane just burns tokens on a comment
     nobody can act on.
   - `backlog` — idea or enhancement without acceptance criteria that is
     not worth elaborating yet: nobody is asking for it, it depends on work
     that has not happened, or it is a nice-to-have you would not schedule
     this quarter. The dividing line against `spec-requested` is intent to
     act, not size.
4. **Correctly labeled and nothing changed?** → `keep`.

`spec-requested` and `plan-requested` cost tokens on the next spec/plan
run, so emit them only once per issue. Before choosing either, read the
issue's comments (`gh issue view <n> --comments`): an issue that already
carries an "## Agent spec" comment has been through the spec lane, and one
that carries "## Agent plan" has been through the plan lane. Re-requesting
a lane that has already reported is never right — if its output was
enough, the verdict is `agent-ready`; if it was not, the verdict is
`needs-human`. Emit at most one gate verdict per issue per run; a single
issue cannot be both specced and planned.

A gate verdict says the issue is *not* ready to implement, so it clears an
existing `agent-ready` label. It leaves `backlog` and `needs-human` in
place — the issue keeps its bucket while it waits in the lane queue.

Closing is the highest bar: only `close-fixed` when you verified the fix in
the code content itself, only `close-invalid` when you can name what makes
it invalid. Before closing, read the issue's comments
(`gh issue view <n> --comments`) — a comment may state a deliberate
hold-open condition (pending device verification, a release, a promotion);
an unmet condition means `keep`, not close. When unsure, choose `keep` or
the safer label — never close on uncertainty.

## Output format

End your final message with a block in exactly this form (nothing after it):

GROOM RESULTS:
#<number> agent-ready|spec-requested|plan-requested|needs-human|backlog|close-fixed|close-invalid|keep — <one concise sentence of reasoning>

One line per issue, every issue accounted for. Use exactly one of those
verdicts — anything else is discarded as unrecognised and the issue is left
untouched.
