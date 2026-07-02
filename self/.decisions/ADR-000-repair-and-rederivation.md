# ADR-000: Repair and re-derivation from OpenAI Symphony

- **Status:** accepted (repair run, 2026-07-01)
- **Scope:** dogfood (`self/`). This is the founding decision record of
  `self/.decisions/`; all later ADRs/AgDRs may reference it as the origin point.

## Context

Switchboard v2 was archived and `main` was reset for a fresh start. Rather than
rebuild the orchestration mechanics from scratch, the repair run re-derived the
methodology and orchestrator layer from **OpenAI Symphony**, vendoring its
orchestration spec as a one-time copy and building Switchboard's own Claude +
GitHub bindings on top. This record captures why that happened and what the
2026-07-01 repair run actually did, so the reasoning is not lost to git
archaeology.

## What the repair run did (commits, by SHA)

Verified against `git log`. Each is cited by short SHA with what it did:

- **`1f578f6`** — restored the kit topology: moved files into their canonical
  homes (`spec/`, `methodology/`, `workflow/`, `scripts/`) and adopted the
  repair-kit docs.
- **`776453f`** — vendored the Symphony orchestration spec at upstream commit
  `4cbe3a96` as a one-time copy and filled in `spec/PROVENANCE.md`.
- **`9e80c8f`** — authored the lost kit files (hooks, scripts, deploy),
  adopted the repair-kit docs, and cleaned up debris.
- **`0561b2f`** — built the Phase-1 Python orchestrator from `SPEC.core`, with
  the Claude and GitHub bindings.
- **`daca3e6`** — resolved all 8 conformance-audit findings against the
  orchestrator.

## Vendored Symphony provenance

Per `spec/PROVENANCE.md`:

- **Upstream:** OpenAI Symphony — `SPEC.md`.
- **Copied at commit:** `4cbe3a9699a73b862466c0b157ceca0c1985d6d7`.
- **License:** Apache License 2.0 (copyright the Symphony authors / OpenAI).
- **Kind:** one-time vendored copy into `spec/SPEC.core.md` — **not** a fork and
  **not** a tracked upstream. There is no sync relationship; `spec/SPEC.md` is
  ours and layers Switchboard's bindings on top. Any future pull-in is a
  deliberate, manual, occasional act (see `spec/PROVENANCE.md` §"Manual upgrade").

## Decisions already recorded under this run

The orchestrator's implementation-defined choices from this run are recorded as
Agent Decision Records (AgDRs) in this directory. This ADR is their anchor:

- `AgDR-001-python-asyncio-single-process.md` — Python + asyncio, one process
  per project.
- `AgDR-002-session-cap-parking.md` — session cap with parking (the one
  sanctioned tracker-write exception); flagged as the run's most contestable call.
- `AgDR-003-reload-mtime-polling.md` — `WORKFLOW.md` reload via per-tick mtime
  polling, no fs-watcher dependency.
- `AgDR-004-permission-posture.md` — agent permission posture (`acceptEdits`
  + git/gh allowlist + PreToolUse containment guard).

## Why record this at all

`self/.decisions/` answers "why we built Switchboard this way." Without a
founding record, the re-derivation looks like an unexplained history rewrite: a
reset `main`, a vendored third-party spec, and a Python orchestrator with no
stated lineage. This ADR is the anchor those later records point back to.
