# AgDR-2026-10-01 - Explicit pilot run identity and read-only title preflight

**Status:** proposed - applies only to the one explicit-Codex pilot launcher
**Date:** 2026-10-01
**Scope:** `scripts/run-self-pilot-checkpoint.sh`; no production provider-routing change.

## Decision

Each `explicit-codex` pilot invocation requires a new, operator-supplied run ID.
The launcher records that validated ID in the issue title and durable checkpoint
record, and rejects any exact all-state title collision. It never resumes or
adopts a prior run.

The accepted form is 1 to 40 lowercase ASCII letters, digits, and internal
hyphens. This keeps the ID safe when quoted into the issue title and record
filename and avoids accepting path separators, shell metacharacters, or
ambiguous leading/trailing hyphens.

## Read-only preflight

`explicit-codex --run-id <id> --preflight` is deliberately limited: after the
existing branch and clean-checkout checks, it makes a read-only exhaustive
all-state issue query and reports only whether the requested title is
available. It exits before the original checkout update, any GitHub write, and
any process launch.

This preflight is not a readiness claim. The actual launch continues through
the original update, host-wide orchestrator exclusivity, environment and label
checks, prerequisite closure, open-Codex-issue check, and branch/PR checks.
`--preflight` is invalid for rollback, and is mutually exclusive with dry-run.

## Collision handling

The collision query uses paginated GitHub API results rather than a fixed
first-page limit, excludes pull requests, and compares the complete title
exactly. A query failure, malformed count, or any collision fails closed. The
same exhaustive check remains in the live path after the original fast-forward
update, so the read-only result cannot authorize a stale or concurrent launch.

## Preserved safety boundaries

This change does not alter provider selection, checkout update order, host
exclusivity, prerequisites, issue or PR gates, durable assignment ordering,
handoff provenance, workspace-cleanliness validation, or the human-only merge
decision. A successful pilot still stops for a human to review and merge its
handoff PR; no code path merges it automatically.

## Verification

Acceptance requires shell syntax validation, mocked subprocess tests for
invalid/duplicate IDs, collision and API-failure fail-closed behavior,
preflight non-mutation, and live guard ordering, plus the full orchestrator
test suite. The operational pilot remains a separately human-gated action.
