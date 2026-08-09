# AgDR-035 — The fold-applied marker, not the body digest, is what says a fold happened

- **Status:** proposed (ratify or overturn at the merge gate)
- **Issue:** #126 (fold-approval loop part b — apply)
- **Touches:** `workflow/WORKFLOW.base.md` (verifier prompt semantics),
  `workflow/transitions.yml` (a new `actor: fold` edge),
  `orchestrator/src/orchestrator/fold_apply.py` (new),
  `scheduler._poll_fold_signals` (consumption point moved)

## Context

Part (a) (#51, AgDR-034) turns an operator's 👍 / `/fold` into a validated
`FoldSignal`. Part (b) has to actually rewrite the issue body, record
provenance, and relabel `drafting → triage` — three writes against an API with
**no conditional update**. GitHub gives us no compare-and-swap, so the loop is
read-compare-write and the TOCTOU window is a residual we accept (single-operator
repo). That forces an explicit answer to one question: *after a crash, how does
the next poll know whether this fold already happened?*

The obvious answer — compare the current body digest to the proposal's
after-sha1 — is wrong, and quietly so. A completed fold's after-digest **survives
an untouched re-triage round**: fold → re-triage routes NEEDS WORK → relabel back
to `drafting` leaves the body digest still equal to after-sha1. Digests alone
cannot distinguish "resume a partial fold from ten minutes ago" from "this
completed weeks ago". Without a separate signal, a stale already-honoured
re-emission re-relabels a live drafting issue into triage.

## Decision

Three coupled choices, all in `fold_apply.py`:

1. **A durable marker comment is the idempotency record.** Its pinned first line
   is `<!-- switchboard:fold-applied verdict:<id> before:<sha1> after:<sha1> -->`,
   matched as a **first-line prefix**, never a substring scan — verdicts embed
   whole revised bodies and bodies quote sentinels, so a substring scan would let
   a comment *quoting* a marker cancel a real fold. The check runs **after
   binding and before any digest comparison**; present ⇒ consume with zero
   writes, whatever the digest says. Digest-based resume/clobber classification
   applies only when the marker is absent.

2. **The marker is keyed on the BOUND (proposal-bearing) comment id, not the
   approved one.** Two signals can reach one proposal by different routes — a
   direct 👍 on the verdict and a 👍 on a fast-path referral that binds forward.
   Their `dedupe_key()`s legitimately differ (the key is signal-scoped). If the
   marker key were signal-scoped too, the second signal would slip past
   marker-first and resume into a spurious relabel. Keying on the fold rather
   than the approval route makes two keys resolve to one fold.

3. **The marker means COMPLETE-OR-TERMINAL.** A relabel failure after body+marker
   is terminal-and-logged, never resumed: the issue stays at `drafting` with a
   folded body, durable provenance, and a loud diagnostic, and the operator
   relabels by hand. This is what makes "marker present ⇒ zero writes" consistent
   with the write order — the alternative would have marker-first suppress a
   retry it had promised to allow.

Write order is **body → marker → relabel**, and the relabel is terminal
*precisely because* it evicts the issue from `FOLD_POLL_STATES`; every failable
step therefore completes while the issue is still pollable.

## Rejected options, steelmanned

- **Digest-only idempotency (no marker).** Genuinely simpler — one fewer write,
  no new comment class for part (a)'s scanner to be shaped around. It reads the
  same state the fold already needs. It fails on the re-triage round-trip above,
  and the failure is silent: the fold looks done, the issue quietly moves states.
  Not worth the simplicity.
- **Marker keyed on `signal.verdict_comment_id`.** Reads more naturally — the
  marker records what the operator approved. But it makes the key a function of
  the approval *route*, and the fallback binding rule (Mechanics 4) deliberately
  lets several routes reach one proposal. Two keys, one body write.
- **Resume the relabel after a marker-recorded failure.** Would close the
  residual in decision 3. It is unreachable: marker-first would suppress the
  re-emission that carries the retry. Making it reachable means a second marker
  class ("body done, relabel pending"), i.e. a state machine in comment bodies.
  The dominant failure mode is the preemption guard firing on a concurrent human
  touch — the operator is already at the issue.
- **A keyed section-replacement payload** (`graph_review.py:42`'s `key=`
  addressing) instead of whole-body replacement. Better blast radius per fold.
  It needs an addressing scheme *and* a merge rule — a different, larger ticket.
  Explicit non-goal here.

## Blast radius

- The issue body becomes agent-writable for the first time. The verifier's
  no-body-edit absolute is **kept** and narrowed by an operator-gated carve-out:
  the verifier's only new output is a proposal block inside its own comment; the
  write belongs exclusively to apply, downstream of the operator's `/fold`.
- `updateIssue(input: {id, body})` is one net-new mutation, under the App's
  existing `issues: write` scope.
- `transitions.yml` gains a second `drafting → triage` edge (`actor: fold`); the
  human edge is kept.
- `fold_signals_seen` now records *decided outcomes* rather than detections, so a
  transient tracker error re-emits. Part (a)'s "one log line per re-emission"
  cost becomes "one apply attempt per re-emission".
- Consumers: the re-triage verifier (fresh Step 0 fetch), #39's churn scheduling
  when it lands (updatedAt bump), operator audit of the marker comment.

## Weakest point

The marker is a comment, and comments are editable and deletable. Delete the
marker and the next re-emission re-folds — the body digest now equals after-sha1,
so the re-entry rule reads it as a partial fold and resumes marker+relabel,
relabelling a possibly-live drafting issue. Nothing in the loop defends against
that; it rests entirely on the single-operator premise. The same premise carries
a second accepted hole: an edit to the bound comment's *proposal block* that
leaves its `body-sha1:` line intact passes re-read revalidation and applies bytes
the operator never saw, because `IssueComment` carries no `updated_at` to check.
If this repo ever gains a second writer, both need revisiting before anything
else here does.
