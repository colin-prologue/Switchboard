# Provenance of the orchestration spec

The orchestration core in `spec/SPEC.core.md` is a **one-time vendored copy**,
not a fork and not a tracked upstream.

- **Source:** OpenAI Symphony — https://github.com/openai/symphony (`SPEC.md`)
- **Copied at commit:** `4cbe3a9699a73b862466c0b157ceca0c1985d6d7` (HEAD of the
  default branch at copy time; fetched via
  `raw.githubusercontent.com/openai/symphony/<sha>/SPEC.md`)
- **Copied on:** 2026-07-01
- **License:** Apache License 2.0 (per `LICENSE` at the copied commit).
  Copyright the Symphony authors/OpenAI. The vendored `SPEC.core.md` is a
  verbatim copy; Switchboard's own bindings and extensions live in
  `spec/SPEC.md`, layered on top. This notice is the required attribution.

## We do not sync

We are diverging substantially for a Claude + GitHub environment, so there is no
rebase relationship to maintain. `spec/SPEC.md` is **ours** and may be edited
freely; `spec/SPEC.core.md` holds the vendored orchestration mechanics we chose to
keep close to the original.

## Manual upgrade (only if ever wanted)

If a later Symphony revision looks worth pulling in:

1. Fetch their current `SPEC.md`.
2. `diff` it against `spec/SPEC.core.md`.
3. Hand-merge only the deltas you want; update the commit SHA and date above.

This is a deliberate, occasional, manual act — not a standing obligation.
