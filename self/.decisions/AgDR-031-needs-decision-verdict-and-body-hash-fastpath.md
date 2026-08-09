# AgDR-031 — NEEDS DECISION verdict class + body-hash unchanged-body fast-path

- **Status:** proposed (ratify at the merge gate)
- **Date:** 2026-08-08
- **Issue:** #55
- **Layers touched:** `methodology/`, `spec/`, `workflow/` (prompt template +
  transition table), `scripts/register-project.sh`

## Context

Issue #15 burned five triage sessions between 2026-07-03 and 07-08 and produced
five concurring NEEDS-WORK verdicts on a body that never changed. The ticket was
blocked on an unmade Gate-A architecture decision — precisely the thing a
verifier is forbidden to make on the operator's behalf. The verdict vocabulary
(PASS / NEEDS WORK / SPLIT) had no way to say "this is stalled on a human
choice", so triage kept re-running the full rubric against an unchanged body at
full session cost, and the actual unblocking conversation happened outside the
ticket.

Two distinct defects, one incident:

1. **No vocabulary for an unmade decision.** NEEDS WORK routes to `drafting` and
   asks the author to *fix* something. There is nothing to fix when the missing
   input is a choice.
2. **No memory of what was already reviewed.** Nothing in a verdict recorded
   *which body* it reviewed, so re-triage could not tell "the author edited"
   from "nothing happened".

## Decision

**A fourth verdict class, `NEEDS DECISION`, routing `status:triage → status:decision`.**
The verifier posts a normal `## Triage verdict` comment (same grep anchor) whose
payload is a decision request: the question, the options each steelmanned, the
per-option acceptance-criteria implications, and "reply on this issue with the
chosen option." The boundary against NEEDS WORK is **determinacy, not
difficulty**: a specification error with a right answer is NEEDS WORK; only a
genuine unmade human choice is NEEDS DECISION.

**`status:decision` is a gate BY OMISSION.** It is not added to `active_states`;
the `active_states` line in `workflow/WORKFLOW.base.md` is byte-identical to
before this change (pinned by `test_active_states_line_is_byte_identical_in_base_and_composed`).
Allowlist semantics mean an absent state is never dispatched — the same
zero-code mechanism that gates `drafting` / `plan-review` / `human-review`.

**Every verdict comment's second line is `body-sha1: <40 hex>`,** computed by ONE
literal command embedded verbatim in the prompt:

```
gh issue view {{ issue.identifier }} --repo {{REPO}} --json body -q .body | git hash-object --stdin
```

**Unchanged-body fast-path.** If the current hash equals the latest verdict's,
the session posts a one-line referral comment and re-routes immediately per the
prior verdict class — no rubric, no re-review. A latest verdict with no parseable
`body-sha1:` line (every pre-#55 verdict) falls through to a full review.

**Exactly two transition edges** (`triage → decision`, `decision → drafting`).
`decision → triage` is deliberately illegal: the chosen option must be folded
into the body first, and the fold path is the existing `drafting → triage` edge.
Allowing the shortcut would re-triage the same unchanged body — the exact loop
this change exists to close.

## Rejected options (steelmanned)

**1. Reuse NEEDS WORK with a "decision needed" convention in the comment body.**
Zero new labels, zero new edges, and the operator still gets the question in the
ticket. This is genuinely the cheapest option and it is what #15 effectively
attempted. Rejected because a convention inside prose is invisible to every
mechanical consumer: the board (#22) cannot render a column for it, the
transition table cannot express it, and — decisively — the ticket sitting at
`status:drafting` looks identical to one awaiting an author's edit, so nothing
distinguishes "waiting on a human choice" from "waiting on a rewrite". The state
is the signal; burying it in text is what produced five sessions.

**2. Add `decision` to `active_states` and give it its own dispatched session
role.** A "decision facilitator" session could chase the operator, summarize
options, and re-triage on answer. Attractive because it keeps the loop moving
without human polling. Rejected because it inverts the gate: an active state is
dispatched, and dispatching a ticket whose only blocker is human judgment burns
sessions producing nothing — the #15 failure mode with extra steps. Gates cost
zero orchestrator code precisely by *not* being dispatched.

**3. `shasum -a 1` (or `sha256sum`) instead of `git hash-object`.** More obvious
to a reader; a plain content digest with no Git framing. Rejected on the
governance constraint: the worker allowlist admits `Bash(git:*)` and
`Bash(gh:*)` only. `shasum` is denied, and a denied command strands the session
mid-verdict — the July-2 permission-wall failure class. `git hash-object --stdin`
buys the same determinism inside the existing allowlist, so the mechanic lands
without widening worker permissions.

**4. Compare `updatedAt` (or the issue's edit history) instead of hashing.**
Free — the tracker already carries it. Rejected because `updatedAt` bumps on
every label write and every comment, including triage's own verdict comment, so
it is guaranteed to differ on re-triage even when the body is byte-identical. It
answers "was the issue touched", not "did the contract change".

**5. Auto-select an option / default on operator silence after N days.** Keeps
throughput up. Rejected as an explicit non-goal: the whole premise is that this
class of question has no determinate answer, so a default is the verifier making
the decision it was forbidden to make, laundered through a timeout.

## Blast radius

- **Prompt template** (`workflow/WORKFLOW.base.md` + the composed
  `projects/switchboard-self/WORKFLOW.md`): every triage session from merge
  onward runs Step 0 and can emit the new verdict. The `## Triage verdict`
  first-line contract is preserved, so `methodology/METHODOLOGY.md:89` and
  `test_prompt.py` grep anchors still find NEEDS DECISION comments.
- **No orchestrator code changed.** `_should_dispatch` excludes `decision` via
  the unchanged `active_states` allowlist; `requires_marker` still keys only
  `todo`.
- **`status:decision` label** provisioned in `scripts/register-project.sh` and
  live on `colin-prologue/Switchboard`.
- **Future consumers:** the board (#22) needs a column when it lands; #51's fold
  CAS reuses the same base-hash concept.
- **Not retroactive:** no past decision-blocked ticket is relabeled and no
  historical verdict is backfilled with a hash — the no-hash fall-through
  handles them by doing a full review.

## Weakest point

**The fast-path trusts a hash the verifier itself wrote, with no cross-check.**
If a session miscomputes or mistypes the digest — or if `gh`'s `-q .body`
output ever differs (trailing-newline handling, CRLF normalization on a body
edited through a different client) — the next session either does a needless
full review (harmless) or, worse, takes the fast-path on a body that *did*
change and re-routes on a stale verdict (harmful, and silent). Nothing validates
the digest against the body at re-route time; the retrofit fall-through only
covers a *missing* hash, not a *wrong* one.

The mitigation is that the failure is one-directional in practice — a differing
hash always means a full review — and that the operator sees the referral
comment naming the prior verdict, so a wrong fast-path is visible in the ticket
rather than silent. But it is real: the first time #51's fold path writes bodies
programmatically, the hash contract should be tightened from "the verifier says
so" to a checked CAS.

A second, smaller weakness: the boundary between NEEDS WORK and NEEDS DECISION
is a judgment call made by the verifier, and a session that finds a hard
question convenient to escalate can park a ticket on the operator instead of
doing the research. The determinacy rule is stated in both the prompt and
METHODOLOGY, but nothing enforces it.
