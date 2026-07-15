# AgDR-021: Use an explicit Python 3 executable in the Codex canary

**Status:** accepted (2026-07-14)
**Surfaces:** Stage 5B Codex workflow template and synthetic canary fixture

## Context

The isolated `switchboard-codex-canary` fixture uses only the Python standard
library and its worker prompt initially instructed the agent to run
`python -m unittest discover -s tests -v`. During fixture seeding, the host
provided `python3` but no `python` executable. Leaving the instruction intact
would turn the first live dispatch into a known environment failure rather than
evidence about the Codex adapter, GitHub handoff, or workspace sandbox.

## Decision

Use `python3 -m unittest discover -s tests -v` in the canonical
`WORKFLOW.codex-canary.md`, its checked-in composed binding, and the fixture
README. A binding test asserts the exact command appears in the rendered prompt.

## Rejected options

- **Keep `python` and let the agent discover the alias issue.** This would test
  prompt recovery, but the initial canary needs a deterministic baseline before
  testing failure handling.
- **Commit a repository-local `python` wrapper.** A wrapper would not normally
  be on `PATH`, adds a non-product artifact, and obscures the actual runtime
  requirement.
- **Add a dependency manager or packaging layer.** The fixture deliberately
  avoids dependencies; a standard-library command is the narrowest signal.

## Blast radius

Only the inert Codex canary prompt and its private synthetic fixture change.
Claude workflows, normal project registration, and production launch commands
are unchanged.

## Weakest point

This proves the current host command convention, not every future Codex runtime.
If a deployed environment lacks `python3`, that is a canary environment failure
to record and investigate rather than a reason to weaken the test command.
