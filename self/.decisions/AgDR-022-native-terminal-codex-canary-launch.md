# AgDR-022: Launch the Stage 5B Codex canary from a native terminal

**Status:** accepted (2026-07-15)
**Surfaces:** Stage 5B operator launch procedure and managed Codex sandbox boundary

## Context

The Codex desktop-managed child session used by the first continuation canary
received a restricted permission profile: the issue workspace was writable but
its `.git` directory was read-only, and approval mode was `never`. It correctly
implemented and tested the scoped change but could not create `.git/index.lock`
to stage it. A prior first-ticket handoff succeeded under a nominally similar
profile, so that success is not reliable evidence of a durable capability.

A disposable probe launched from the macOS Terminal app, using ChatGPT
subscription login, `--ask-for-approval never`, `--ignore-user-config`, and
`--sandbox workspace-write`, created and committed `PROBE.md` successfully.
It did not use a sandbox bypass or access a production repository.

## Decision

Run Stage 5B foreground Codex workers from a native macOS terminal, with the
bundled Codex CLI directory on `PATH`, rather than launching the pool as a
child of a Codex desktop task. Keep `workspace-write` and approval `never`;
do not grant full filesystem access or use a dangerous bypass. This is an
operator boundary for the isolated canary, not a production rollout decision.

## Rejected options

- **Use the desktop-managed child profile.** Its explicit read-only `.git`
  entry prevents the required commit/push handoff.
- **Use `danger-full-access` or a sandbox bypass.** That solves the symptom by
  expanding the worker beyond the canary's agreed containment boundary.
- **Manually commit the retained issue #3 diff.** That would erase the canary's
  evidence and would not prove a worker can hand off autonomously.
- **Require interactive approval for Git.** A headless pool cannot depend on a
  person being present for every commit.

## Blast radius

Claude production processes and all checked-in workflow safety settings remain
unchanged. Only the operator terminal used for the isolated Codex canary needs
the bundled CLI path exported before starting `scripts/run-project.sh`.

## Weakest point

The probe proves a disposable local Git commit, not a full GitHub handoff or
restart recovery. Resume issue #3 from its preserved workspace under the native
terminal before treating the environment boundary as fully cleared.
