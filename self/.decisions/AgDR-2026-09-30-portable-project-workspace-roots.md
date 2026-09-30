# AgDR-2026-09-30 - Portable project workspace roots

**Status:** accepted
**Date:** 2026-09-30
**Scope:** tracked project bindings and their committed workflow snapshots.

## Decision

Project workspace roots use $HOME/Developer/switchboard-workspaces/<slug>.
The matching committed WORKFLOW.md snapshots carry the same expanded-root
expression so setup verification and runtime composition remain in sync.

## Incident

After the Sonnet 5.5 deployment moved the WSL host to merged main, both
bindings still used /Users/colindwan/.... The switchboard-self service then
failed before dispatch with mkdir permission denied; CIV-LIFE was not started.
No queue work or ticket state changed as part of the failure.

## Rationale

One WSL machine owns one runner per project, but its home directory is not a
macOS path. A binding must derive its workspace root from the host environment
rather than from the developer machine that last generated it.

## Verification

- A regression test rejects non-portable tracked workspace roots for both
  bindings.
- Existing setup and workflow-composition tests verify the generated snapshots.
- The full orchestrator suite passes after the correction.

## Consequence

- The controlled runner restart remains blocked until this recovery change has passed review and its normal publication gate.
