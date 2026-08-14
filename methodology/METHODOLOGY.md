# Switchboard Methodology (IDSD on Symphony)

This is the human/agent workflow Symphony enforces. It encodes the IDSD layer
split — humans author **Intent** and **Spec**; the system owns **Implementation**
— as GitHub issue **states** (status labels) and gates. The orchestrator only
dispatches *active* states and parks at *gate* states, so every gate costs zero
orchestrator code.

## States (status labels)

| Label                  | Active? | Meaning                                                        |
|------------------------|---------|----------------------------------------------------------------|
| `status:drafting`      | no      | Gate A pending — intent + spec being authored/approved         |
| `status:triage`        | **yes** | Adversarial ticket verification — dispatched to a verifier session |
| `status:todo`          | **yes** | Approved, unblocked, dispatchable                              |
| `status:in-progress`   | **yes** | An agent is working it                                          |
| `status:decision`      | no      | Waiting on the operator — triage asked a Gate-A question (issue #55) |
| `status:plan-review`   | no      | Gate B handoff — agent produced a plan/ADR awaiting approval    |
| `status:human-review`  | no      | Gate C handoff — implementation done, awaiting human merge      |
| `status:blocked`       | no      | Parked (fallback when native dependencies aren't available)     |
| *(issue closed)*       | —       | Terminal                                                       |

Dependencies use GitHub's native **blocked-by**; Symphony won't dispatch a
`status:todo` issue while any blocker is unresolved.

### Who writes which status label (four writers)

One status label per issue is the workflow contract, and each label has exactly
one owner. Worker agents write **no** status labels at all (issue #61 /
AgDR-028): a worker's final action is the handoff evidence file, and the
orchestrator performs the verified transition.

| Label(s)                                        | Written by | When |
|-------------------------------------------------|------------|------|
| `status:drafting`, `status:plan-review`, `status:blocked` | **humans** | authoring/approving at the gates |
| `status:triage` → `status:todo` \| `status:drafting`      | the **triage verifier agent** | on its PASS / NEEDS WORK / SPLIT verdict |
| `status:triage` → `status:decision`             | the **triage verifier agent** | on its NEEDS DECISION verdict (issue #55) — the ticket is blocked on an unmade human decision |
| `status:decision` → `status:drafting`           | **humans** | the operator picked an option; the answer is folded into the body at drafting (manual until #51) |
| `status:human-review`                           | the **orchestrator** | after provider-turn success + validated handoff evidence (issue #61 / AgDR-028; workers only write `.run/handoff-evidence.json`) |
| `status:todo` → `status:in-progress`, its revert, and `status:parked` | the **orchestrator** | claim taken / claim died / session cap |

`status:in-progress` is **board visibility only, not a lock** — a label cannot
compare-and-swap, so cross-runner mutual exclusion is a separate concern
(issue #15). The orchestrator applies it once when a `todo` issue is first
claimed and clears it when the claim genuinely dies (mid-run release, or a
startup sweep of claims stranded by a crash). A handoff to `status:human-review`
is observed, never reverted: any status label other than a sole `status:in-progress`
means a human/agent already moved the issue, so the orchestrator leaves it alone.

> **Config caveat (single-runner assumption).** The `status:in-progress` swap is
> safe under this repo's config because eligibility uses empty `required_labels`
> and `"in progress"` is itself an active state, so the orchestrator's own write
> keeps the issue eligible on the retry path. A config that set
> `required_labels: ["status:todo"]` would make the orchestrator self-release on
> its own write (the label it just removed is the one it now requires) — that
> combination is unsupported. The startup sweep's revert of stranded claims also
> assumes **one runner per repo**; if multi-runner lands (issue #15), the sweep
> must be re-gated so it cannot revert a live peer's claim.

## Gates

- **Gate A — intent/spec approved.** A ticket sits at `status:drafting` until a
  human approves its task-intent and acceptance criteria, then moves it to
  `status:todo`. The agent never sees an unapproved ticket.
- **Gate B — plan/architecture approved.** For architecture-touching work, the
  agent produces an implementation plan + ADR, parks at `status:plan-review`, and
  a human approves before child tickets are filed.
- **Gate C — final review.** Every implementation hands off at
  `status:human-review`. A human merges. Agents never self-merge. Merge review
  includes ratifying (or overturning) any AgDRs the PR added under
  `<convention_root>.decisions/` — a PR that changed spec/methodology
  semantics without one is incomplete. It also checks the `## In brief` block
  (see "Writing for the reader" below): a PR body with no block, or whose
  **What could be wrong** names a quality rather than a consequence, is
  incomplete the same way and bounces the same way.

## Triage — adversarial ticket verification (active state)

`status:triage` moves "verification before autonomy" to the ticket layer: before
an issue becomes dispatchable, an independent verifier session subjects it to
adversarial scrutiny so the implementing agent only ever sees contracts that
survived independent review. It is an **active** state (Symphony dispatches it),
but the dispatched session runs as a *verifier*, not an implementer — the
`status:triage` branch in the workflow prompt swaps the role. It reuses the
same dispatch machinery and session shape as an implementation session, but
**not** the same budget: verify and implement sessions are capped independently
(same cap value, separate counters — issue #35 / AgDR-030), so verification
passes never eat the implementation budget. It also leans on one generic
scheduler rule: sessions are *role-pinned* — when
a worker's issue changes state (even active → active, e.g. a PASS relabel
`status:triage → status:todo`), the session ends at the next turn boundary and
normal re-dispatch starts a fresh session in the new role (SPEC.md §4).

The verifier applies the rubric in the prompt body (assumptions, criteria shape,
testing asks, sizing, boundaries) and routes to exactly one verdict:

- **PASS** → relabel `status:triage → status:todo` (dispatchable).
- **NEEDS WORK** → relabel `status:triage → status:drafting` + a `## Triage
  verdict` feedback comment (fixed, grep-able heading). This is the verdict for a
  **specification error with a determinate answer** — an unstated assumption, an
  unbounded criterion, a drifted citation.
- **NEEDS DECISION** → relabel `status:triage → status:decision` + a `## Triage
  verdict` comment carrying the decision request (the question, the options each
  steelmanned, per-option acceptance-criteria implications, and "reply on this
  issue with the chosen option"). This is the verdict for an **unmade human
  decision** — a Gate-A architecture choice the ticket is stalled on, which a
  verifier is rightly forbidden to make on the operator's behalf. The boundary
  against NEEDS WORK is determinacy, not difficulty: a question with a right
  answer is NEEDS WORK; a question with a *choice* is NEEDS DECISION. Issue #15
  burned five triage sessions on five concurring NEEDS-WORK verdicts for want of
  this class.
- **SPLIT** → file child issues at `status:drafting` (drafted bodies, native
  blocked-by chaining), park the parent at `status:drafting`.

Every verdict comment's first line is `## Triage verdict` and its second line is
`body-sha1: <40 hex>` — the hash of the body that verdict reviewed, computed by
the one literal command the prompt embeds (`gh issue view … --json body -q .body
| git hash-object --stdin`; `git hash-object` is allowlisted, `shasum` is not).
A re-triaged issue whose current body hash equals the latest verdict's takes the
**unchanged-body fast-path**: one referral comment and an immediate re-route per
the prior verdict class, no re-review. A latest verdict with no parseable
`body-sha1:` line (every pre-#55 verdict) falls through to a full review.

The verifier never edits the issue body and never writes feature code — comments,
labels, and child issues only. Transitions in: a human (or a `SPLIT` parent)
files at `status:triage`. Transitions out: `status:todo`, `status:drafting`, or
`status:decision`. `status:decision` leaves only via `status:drafting` (the
operator's answer must be folded into the body before re-triage —
`decision → triage` is deliberately illegal; see `workflow/transitions.yml`).

### When to file at `status:triage` vs straight to `status:todo`

Proportionality applies here too — triage is a scrutiny gate, not a mandatory
tollbooth:

- **Skip triage** for trivial/low-risk tickets whose criteria are already
  bounded and checkable (a one-line fix, a typo, a config bump). File them
  straight at `status:todo`. Forcing triage onto a five-minute bug is the same
  mis-set-entry-state mistake as forcing Gate A/B onto it. Use
  `scripts/new-ticket.sh --entry todo` — it stamps the `gate:triage-passed`
  marker alongside the label (the filer is the out-of-band verification;
  the dispatch guard refuses an unstamped `status:todo`).
- **File at `status:triage`** when a ticket is new, author-fresh, or its criteria
  smell unbounded ("all/every/comprehensive"), its assumptions are unstated, or
  its size is uncertain — exactly the cases where an unverified contract can burn
  an implementation session. (Calibration pair: a bounded ticket round-trips
  cleanly; an unbounded one burns a session and gets parked.)

## Proportionality (the risk knob)

The path a ticket takes through states *is* the risk control:

- **Routine / low-risk** (a bug, a small change): file it directly at
  `status:todo` with a one-line task-intent. No product-intent tier, no Gate A/B.
  This is the Symphony-light path — fast, the common case.
- **Architecture-touching or long-lived:** file at `status:drafting`; it flows
  `drafting → todo → (plan-review) → human-review`, and it carries a
  `parent-intent: <slug>` pointer to a product-intent file holding the durable
  NFR/environment constraints.

If you find yourself forcing heavy ceremony onto a five-minute bug, you've mis-set
the entry state. Match the path to the risk.

## Task-intent / spec in the issue body

For gated work, the issue body should contain:

- an `## In brief` block (see "Writing for the reader" below) as the first
  section,
- a one-paragraph **intent** (what + why),
- **acceptance criteria** written as checks (pass/fail, eval-shaped),
- **non-goals** (hard scope boundaries),
- **assumptions** (things taken as given; if false, the ticket is void),
- a `parent-intent: <slug>` line if it inherits a product-intent file.

Acceptance criteria are the agent's definition of done; non-goals are boundaries
it must not cross. (Product-intent files, the verification contract, and the
elicitation front door arrive in later roadmap phases.)

## Writing for the reader — the `## In brief` block

Everything else in this methodology optimizes for an implementing agent: exact
citations, enumerated consumers, `file:line` at a named sha. That precision is
load-bearing and stays. It is also unreadable to a human catching up — the
operator between context switches, or somebody helping review the board.

So every agent-written ticket body, PR body, and judgment-carrying triage
verdict opens with a fixed, grep-able block carrying **insight, not
information**:

```
## In brief

**What this does:** <one sentence>

**What could be wrong:** <one decision or assumption, and what breaks if it is
false>
```

**"Judgment-carrying" excludes exactly two of the five verdict routes.** NEEDS
WORK, NEEDS DECISION, and SPLIT each reach a conclusion a reader can argue with,
so each carries the block. A PASS verdict is one line saying the ticket passed,
and an unchanged-body fast-path referral re-routes per a prior verdict having
run no rubric and produced no new findings by construction. Neither holds
judgment for a reader to scrutinize, so on those two a block would be ceremony
wrapped around nothing — and the surest way to teach readers to skip it.

Two rules make the fields hard to pad, and they are the whole mechanism:

1. **"What this does" bans identifiers** — no issue numbers, no file paths, no
   AgDR/ADR/OBS identifiers, no `status:*` label names, no function, class, or
   field names. An author who cannot clear this bar has not understood its own
   change well enough to summarize it. This is what buys the twenty-second
   glance.

2. **"What could be wrong" requires a conditional and a consequence** — the
   *if X, then Y* shape. Naming a quality ("coverage could be broader", "this
   could be more robust") fails; naming a trigger and its damage passes. This is
   the scrutiny surface: it tells a reviewer what to argue with before merging.

**Placement.** The block is the first section, with exactly two exceptions, each
for its own reason. On a PR body, the `Closes #N` line comes first: the
orchestrator resolves the issue link through GitHub's closing references, which
match a closing keyword anywhere in the body — so the line's **presence** is
machine-enforced by the handoff check, but its **position** is convention only,
kept first so it stays visible and never gets edited away. On a triage verdict,
**two** lines precede the block and the block is third, because both of those
lines are contracts rather than courtesies: the `## Triage verdict` heading,
which the workflow prompt pins as the comment's first line so verdicts stay
grep-able, and the `body-sha1:` line directly under it, which the next triage
session parses to tell a revised body from an unchanged one. A human-facing
layer never displaces a machine-read one. Everything the author would otherwise
write goes below the block, unchanged. The block adds a layer; it removes
nothing.

**Enforcement is asymmetric on purpose.** PR bodies are gated at Gate C — a
missing or hedged block bounces the PR, the same as a missing AgDR. Ticket
bodies and triage verdicts get the block from their templates but are never
bounced for it: a triage round costs a full dispatched session, and a bounce for
prose does not reduce implementation risk.

The orchestrator's own generated comments (park notices, dispatch refusals) need
no block — they already lead with a plain sentence and a concrete next action,
and are the model this section generalizes.

## Drafting-quality checklist — the recurring failure classes

Issue #14 took four triage rounds to reach dispatch; eight of its nine findings
collapse into a handful of failure classes that are checkable at *drafting* time,
not rediscovered one triage round at a time. Encode them here (prose for readers)
and in the executable surfaces that reach every author — the `new-ticket.sh
--scaffold` skeleton and the `status:triage` rubric — so drafting and triage share
one vocabulary. (Attribution, not a pass condition: OBS-023 is the fake-fidelity
observation these rules generalize; issue #14's four-pass verdict trail is the
worked example that motivated them. Neither is resolvable inside a workspace
clone, so treat them as provenance only.)

1. **Claim-vs-code drift.** Every cited mechanism carries a `file:line` verified
   at a named HEAD sha, or is explicitly labeled a guess. A ticket that cites a
   transition table, a re-fetch, or a "reused" sweep that does not exist at HEAD
   burns the implementing session rediscovering that the claim is fiction.

2. **Consumers of mutated state.** For any state a ticket mutates — a `status:*`
   label, issue state, a workspace, an env var — enumerate *who else reads it and
   how*. This is one question asked repeatedly across #14's deepest findings.
   Worked example: a ticket that writes a `status:*` label must enumerate the
   eligibility/dispatch path (does relabeling make the issue dispatchable, or
   pull it from the active set?), the between-turn role-pin check (a state change
   ends the pinned session at the next turn boundary — see AgDR-005), and any
   `updatedAt` consumers (a label write bumps the issue's `updatedAt`, which
   ordering/polling logic may key on).

3. **Fake fidelity.** *Any state the real system derives, the fake must derive the
   same way.* A fake that hard-codes what the real system computes passes its own
   tests and lies about the system. Known instances: a comment write echoes the
   server-assigned `updatedAt` (the fake must echo it, not invent one); an issue's
   `state` is recomputed from its `status:*` labels (the fake must recompute it
   from labels, not store a separate field).

4. **AC executability under the worker's capability envelope.** Every acceptance
   criterion names a command the dispatched agent can actually run under the
   worker allowlist (`workflow/WORKFLOW.base.md:61`: `git`, `gh`, and the two
   pinned `uv run --project orchestrator ... pytest` prefixes), *or* explicitly
   assigns that step to the human merge gate. An AC naming a command outside the
   allowlist (a bare `pytest`, a `register-project.sh` run, a `cd … &&` chain) is
   unsatisfiable at runtime and strands the session — the July-2 #10/#11
   permission-wall incident, and #14's AC3 as first drafted.
