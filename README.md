# agent-ops

agent-ops is a deliberately small agent factory built from reusable GitHub
Actions workflows. It runs unattended in GitHub, uses GitHub as its state
store, and contains no local CLI, Python package, TUI, worktree manager, or
custom orchestration framework.

## The whole lifecycle

```text
Discover & Plan → Implement → Review & Release → human merge
```

| Lane | Input | Output | Default model |
| --- | --- | --- | --- |
| Discover & Plan | Repository signals and incomplete issues | A deduplicated `agent:ready` issue | `fable` |
| Implement | Oldest `agent:ready` issue | A separate branch and draft PR | `sonnet` |
| Review & Release | Oldest PR labeled `agent:review` | `agent:approved` or `agent:changes-requested` | `opus` |

There is no distill, evolve, staging, promotion, auto-merge, local lane, or
secondary database.

## Authentication and billing

The workflows use `CLAUDE_CODE_OAUTH_TOKEN`, generated with:

```sh
claude setup-token
```

Store it as a GitHub Actions secret in every target repository. This uses the
Claude subscription rather than an Anthropic API key.

The implementation workflow additionally uses a custom GitHub App
installation token. Store its App ID and private key as `AGENT_APP_ID` and
`AGENT_APP_PRIVATE_KEY`. Give the App only these repository permissions:

- Contents: read and write
- Issues: read and write
- Pull requests: read and write
- Metadata: read

The App token is not model billing. It gives each implementation run a
short-lived, repository-scoped GitHub identity so the pushed branch and
opened PR trigger normal CI. A PR created with the workflow's ordinary
`GITHUB_TOKEN` may leave those downstream runs awaiting approval or
suppressed.

Do not add `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or copied personal Codex
session files. The official Codex GitHub Action currently requires API-key
billing, so GPT Sol is intentionally not wired into unattended
implementation. Switching the implementation model to Codex later is one
workflow-boundary change when official subscription authentication exists.

## Install in a target repository

This control repository must remain public when private target repositories
span different users or organizations. GitHub does not share a private
reusable workflow across unrelated owners. If the control repository must be
private, keep one copy per owner or copy the three complete workflows into
each target repository.

Copy the callers:

```sh
mkdir -p .github/workflows
cp templates/workflows/agent-discover-plan.yml .github/workflows/
cp templates/workflows/agent-implement.yml .github/workflows/
cp templates/workflows/agent-review-release.yml .github/workflows/
```

When onboarding another repository, copy from this repository rather than
running those commands literally inside the target checkout.

The callers reference the reviewed `v1` tag. Changes merged to `main` do not
reach target repositories until that tag is moved; moving `v1` is a deliberate
release step.

Then:

1. Add the three secrets described above.
2. Confirm the custom GitHub App is installed on the target repository.
3. Protect `main`: require a PR, required CI checks, and human approval.
4. Adjust the caller schedules and Claude model aliases if necessary.
5. Run Discover & Plan manually first.
6. Inspect the first implementation draft PR before enabling its schedule.
7. Enable Review & Release only after implementation behavior is trusted.

## State

The workflows use these labels, with explicit transitions that remove them:

- `agent:needs-plan`: Discover & Plan removes it when promoting the issue to
  `agent:ready`.
- `agent:ready`: Implement removes it after opening the draft PR, or when a
  failed run moves the issue to `agent:blocked`.
- `agent:implementing`: Implement removes it after opening the draft PR, or
  when the run fails.
- `agent:review`: Review & Release removes it when recording a verdict, or
  when a failed review moves the PR to `agent:blocked`.
- `agent:approved`: Review & Release removes it when a later verdict requests
  changes.
- `agent:changes-requested`: Review & Release removes it when a later verdict
  approves the revision.
- `agent:blocked`: a human removes it when requeuing an issue with
  `agent:ready` or retrying a PR with `agent:review`.

The workflows create missing labels. Discover & Plan moves work toward
`agent:ready`; Implement claims one issue and opens a draft PR from an
`agent/issue-...` branch; Review & Release performs a fresh independent pass.
Only a human merges.

The Discover & Plan caller also performs deterministic intake. When an issue
is opened by an owner, member, or collaborator, it adds `agent:needs-plan`
without invoking a model. The next scheduled or manual Discover & Plan run
assesses the issue. Public contributors are not automatically queued.

If review requests changes, it adds `agent:changes-requested`. After a human
pushes a revision, that human restores `agent:review` to request a new
independent pass.

## Repository map

```text
.github/workflows/   three reusable lifecycle workflows
prompts/             one judgment prompt per lifecycle lane
templates/workflows/ small scheduled callers for target repositories
docs/architecture.md state and trust boundaries
```

## Validation

There is no build or dependency installation. Validate workflow syntax with:

```sh
actionlint -color -shellcheck=
```

Workflow and prompt changes affect unattended agents and must be reviewed by
a human before merge.
