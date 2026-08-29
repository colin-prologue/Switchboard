# Switchboard Methodology (IDSD on Symphony)

This is the human/agent workflow Symphony enforces. It encodes the IDSD layer
split — humans author **Intent** and **Spec**; the system owns **Implementation**
— as GitHub issue **states** (status labels) and gates. The orchestrator only
dispatches *active* states and parks at *gate* states, so every gate costs zero
orchestrator code.

## States (status labels)

"Active" means **dispatched by the scheduler**; everything else parks. Which
states are active is the **stance's** choice (see Proportionality), so the column
below is per stance rather than absolute. `base` is the pre-stance pipeline,
still the default for projects registered before the ladder.

| Label                  | `base` | `prototype` | Meaning                                                        |
|------------------------|--------|-------------|----------------------------------------------------------------|
| `status:drafting`      | no     | *unused*    | Gate A pending — intent + spec being authored/approved         |
| `status:triage`        | **yes**| *unused*    | Adversarial ticket verification — dispatched to a verifier session |
| `status:todo`          | **yes**| **yes**     | Approved, unblocked, dispatchable                              |
| `status:in-progress`   | **yes**| **yes**     | An agent is working it                                          |
| `status:review`        | —      | **yes**     | Gate C handoff to an **agent** reviewer — PR open, awaiting QA  |
| `status:decision`      | no     | *unused*    | Waiting on the operator — triage asked a Gate-A question (issue #55) |
| `status:plan-review`   | no     | *unused*    | Gate B handoff — agent produced a plan/ADR awaiting approval    |
| `status:human-review`  | no     | no          | Gate C handoff to a **human** — awaiting human merge            |
| `status:blocked`       | no     | no          | Parked (fallback when native dependencies aren't available)     |
| *(issue closed)*       | —      | —           | Terminal                                                       |

*unused* means the stance's prompt never routes to that state and its machinery
is unreferenced — **not removed**. Re-stancing restores it.

Both Gate C states can coexist on one board: `prototype` hands off to
`status:review`, and its QA role escalates to `status:human-review` for anything
on its escalation list. `status:human-review` is inactive at every stance by
design — it is the human gate, and a gate is a state nobody dispatches.

Dependencies use GitHub's native **blocked-by**; Symphony won't dispatch a
`status:todo` issue while any blocker is unresolved.

### Who writes which status label (five writers)

One status label per issue is the workflow contract, and each label has exactly
one owner. **Implementing** agents write no status labels at all (issue #61 /
AgDR-028): a worker's final action is the handoff evidence file, and the
orchestrator performs the verified transition. A **reviewing** agent is a
different role and does write them — the same way the triage verifier always
has.

| Label(s)                                        | Written by | When |
|-------------------------------------------------|------------|------|
| `status:drafting`, `status:plan-review`, `status:blocked` | **humans** | authoring/approving at the gates |
| `status:triage` → `status:todo` \| `status:drafting`      | the **triage verifier agent** | on its PASS / NEEDS WORK / SPLIT verdict |
| `status:triage` → `status:decision`             | the **triage verifier agent** | on its NEEDS DECISION verdict (issue #55) — the ticket is blocked on an unmade human decision |
| `status:decision` → `status:drafting`           | **humans** | the operator picked an option; the answer is folded into the body at drafting. **Manual by design, not pending** — #51/#126 shipped and `fold_apply` explicitly declines this state (`decision → triage` is illegal, and a NEEDS-DECISION verdict predates the answer so it carries no proposal) |
| the stance's `handoff_label` — `status:human-review` by default, `status:review` at `prototype` | the **orchestrator** | after provider-turn success + validated handoff evidence (issue #61 / AgDR-028; workers only write `.run/handoff-evidence.json`). The *target* is config (AgDR-039); the validation before writing it is unchanged |
| `status:review` → `status:todo` \| `status:human-review` | the **QA agent** | on its FIX verdict (back for another pass) or ESCALATE (something on the escalation list, or a finding surviving two rounds). On SHIP it merges and writes no label — the merge closes the issue |
| `status:human-review` → `status:todo` | **humans** *or* the **orchestrator** — **two actors, one edge** (`transitions.yml`) | the human path is a changes-requested verdict: the reviewer relabels and the ticket re-enters dispatch without re-triage. The orchestrator path is the review-response sub-poll, when a bound PR carries an unresolved bot review thread whose last bot comment postdates Switchboard's reply (issue #43 / AgDR-037; `scheduler.py`). Either way it is an ordinary implement-role session and the `gate:triage-passed` marker survives. **Session counters reset only on the orchestrator path** (`_reset_issue_sessions`, `scheduler.py`); a human relabel does not reset them, so a ticket that already spent its implement budget parks on re-dispatch — issue #178 |
| `status:drafting` → `status:triage` | the **orchestrator** | fold apply: the operator approved a triage verdict, the body was rewritten under a base-sha1 CAS, and the ticket goes back for re-triage (issue #126 / AgDR-035; `fold_apply.py`) |
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
- **Gate C — final review.** Every implementation hands off for review before it
  merges. **Nothing merges unreviewed** — that is the guarantee, and it holds at
  every stance.

  **Who performs the review is the stance's business.** A gated stance hands off
  to `status:human-review`, which no stance dispatches, so the work parks until a
  human merges. An autonomous stance hands off to a QA state it *does* dispatch
  (e.g. `status:review`), and a reviewer session merges within a bounded
  escalation list, routing anything on that list to a human instead.

  The handoff target is the stance's `tracker.handoff_label`, and the loader
  refuses a non-default target absent from `active_states` — otherwise completed
  work would park somewhere nothing dispatches and never be seen again.

  Two review duties, and they do **not** both transfer to an agent reviewer,
  because they are different kinds of act:

  - **The `## In brief` block is checked at every stance, by whoever reviews.**
    A PR body with no block, or whose **What could be wrong** names a quality
    rather than a consequence, is incomplete and bounces. This is compliance
    against two fixed rules (see "Writing for the reader" below) — cheap, and
    checkable without deciding anything about the project's direction.

  - **Ratifying (or overturning) an AgDR is a human act, and stays one.** A PR
    that changed spec/methodology semantics without a decision record is
    incomplete — but *accepting* that record is a judgement about where the
    project is going, which the operator owns. A peer agent cannot ratify on
    the operator's behalf, so at a stance whose Gate C is an agent reviewer,
    AgDRs are written and merged, then reviewed by the operator out of band.
    A stance that wants them gate-blocked must route to `status:human-review`.

  Do not read the first bullet as the weaker one: it is the block the operator
  actually reads, so enforcing it is what keeps a merged board legible.

  > **Why this is a reframing rather than a loosening.** Earlier revisions said
  > "a human merges; agents never self-merge." What Gate C protects is that
  > nothing reaches the base branch unreviewed — not that a particular species
  > clicks the button. Stating it as an absolute would have forced an exception
  > per stance, and three provisos hanging off one sentence is how a guarantee
  > stops being read. See `AgDR-039`.

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

Two scales, one argument: **match the path to the risk.** A ticket's path through
the states is the per-ticket knob; a project's **stance** is the same knob one
level up.

### Per project — the stance

A project declares how much of this methodology is live, via
`SB_WORKFLOW_STANCE` in its `project.env`. The stance selects a workflow recipe
(`workflow/stances/WORKFLOW.<stance>.md`, or a project-local override beside the
binding), and the recipe's `active_states` decides which states below are
dispatched and which park.

**A gate is not code — it is a state absent from `active_states`.** The scheduler
walks past it and the ticket sits. That is the whole mechanism, and it is why
adding or removing a gate costs no orchestrator changes.

| Stance | For | Shape |
|--------|-----|-------|
| `prototype` | You do not know what you are building yet. A bad merge costs one `git revert` on a repo nobody depends on. | No triage, no Gate A/B, no fold loop. Three inline preflight checks instead of the triage rubric. A QA session reviews and merges; only the escalation list reaches a human. |
| `harden` | You have found it and are consolidating. | *Not yet written.* Verification returns; contracts get pinned. |
| `sustain` | Something outside the project depends on it. | *Not yet written.* The human gate returns where blast radius warrants. |

Only `prototype` ships today. `harden` and `sustain` are deliberately unwritten
until a real project has run under `prototype` long enough to say what tightening
should mean — inventing them now would be guessing.

Machinery a stance omits is **unreferenced, not deleted**. Re-stancing is a
template swap plus a recompose; the orchestrator hot-reloads on the composed
workflow's mtime, so it takes effect without a restart. A project may also move
*down* the ladder for an exploratory push — stance is a property of the work in
front of you, not a rank the project earns.

### Writing a stance: it is not just prompt text

Every stance shipped so far needed runtime changes the prompt could not supply,
and each one was discovered in production rather than at review. Before a new
recipe is considered done, walk this list:

1. **Every state the prompt names must be in `active_states`, or nothing
   dispatches it.** A gate is a state nobody dispatches — so a prompt that tells
   an agent to pick work up at `status:review` describes a gate unless the recipe
   also lists `review` as active.
2. **Set `handoff_label` if the stance ends anywhere but the human gate.** The
   orchestrator writes this on a validated handoff; leaving the default parks
   completed work at `status:human-review` no matter what the prompt says. The
   loader refuses a non-default target absent from `active_states`, which is the
   check that catches (1) and (2) together.
3. **Check the session-role split covers the new states.** Per-role budgets key
   on `(issue, role)`. A new state that is not registered as its own role shares
   the implementer's budget and exhausts it early — the QA role ran as
   `implement` for exactly this reason.
4. **Ask what the stance implies about *permissions*, not just flow.** Handing
   Gate C to an agent means the merge guard has to know. A stance that changes
   who reviews and does not change what the tool layer permits will be silently
   overridden by the tool layer.
5. **Compose it once and read the output.** Substituting a value into a
   YAML scalar can produce invalid YAML that the template alone never shows.

The pattern behind all five: **a stance adds a concept the runtime usually has a
slot for already.** The question to ask of each new prompt instruction is not
"is this clear?" but "which existing runtime field carries this, and did I set
it?"

Rationale and refutation: `self/.decisions/AgDR-039-per-project-stance-ladder.md`.
The permission dimension in (4) is `self/.decisions/AgDR-043-gate-c-owner-is-a-stance-property.md`.

### Per ticket — the entry state

Within whatever stance is live, the path a ticket takes through states *is* the
risk control.

**The entry state is not a constant, because the states are not.** A ticket filed
at a state its project's stance does not dispatch is neither active, nor a gate,
nor terminal: nothing moves it and nothing is waiting for it. So `new-ticket.sh`
resolves the target project's `active_states` (the same repo→binding→composed-
`WORKFLOW.md` walk the board sync uses) and defaults to `triage` where the
project verifies before dispatch, `todo` where it does not, and refuses outright
where it cannot tell — naming `--entry` as the fix. An explicit `--entry` naming
a state the project neither dispatches nor declares as a gate is refused the same
way. See `AgDR-049`; the two defaults that composed into a dead ticket were
issue #176.

At a stance that dispatches them, the options are:

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

---

## Decisions are standing predictions, not justifications

A decision record here is not a defence of a past choice. It is a **claim about
the future with the conditions for its own falsification attached** — which is
why every record carries a `Weakest point` or `What would make this wrong`
section, and why writing that section honestly is the hardest part of drafting
one.

That framing has a consequence the practice was missing: **those sections are
predictions nobody re-reads.** They were written well and then filed.

### Conflict is evidence, not failure

When two parts of the system reach different conclusions about the same thing,
the useful response is not "which one is broken?" but **"why did two careful
readings diverge, and which assumption should update?"**

Almost every defect this project has found is a *second-reader* problem: a fact
recorded correctly in one place, then a second consumer appearing that either
contradicted it or never learned it existed. The merge guard and the scheduler
holding different beliefs about Gate C. The board sync deriving states from one
template after states became per-project. A setup guide that never learned
stances existed.

Those are not carelessness. They are the signature of work built by sessions
with perfect local context and no memory of what else is in flight — so remedies
that depend on *remembering* will fail the same way. Only artifacts that fail
loudly survive.

The corollary for records: **easy to supersede, hard to silently contradict.**
Amend in place with a dated header naming the new authority; do not rewrite and
do not delete. A rewrite destroys the thing that makes the next conflict
legible — what was believed, and why it changed.

### The sweep

Periodically, read every record's refutation section against what has actually
happened since. This is cheap — the hard half (writing a falsifiable condition)
is already done — and it is the highest-yield review available, because it finds
**assumptions the world has already falsified** rather than code that drifted.

The first sweep (2026-08-15, 45 records) found five fired:

- `AgDR-013` named "an agent retrying a denied command" as the trigger for
  promoting the cap-hit ticket. It happened twice in one day.
- `AgDR-017` predicted the dual-read compatibility layer would become permanent
  absent a removal criterion. No criterion was ever set.
- `AgDR-033` named the hard-coded verify-state set as "the seam that will need
  reopening". It reopened — **and the first fix added a second literal to the
  same list**, which is the part worth internalising: a prediction firing does
  not guarantee the response addresses what was predicted.
- `AgDR-040`'s premise about an external reviewer was falsified twice in one
  evening, once by the very PR correcting it.
- `AgDR-036`'s load-bearing premise turned out never to have been verified at
  all, despite the record assigning the check to a named gate.

That last one is the pattern to watch for specifically: **a record that assigns
verification to a human gate and is then merged without it.** The assignment
reads as diligence and produces nothing.

### What NOT to do with this

Do not convert prose into tests wholesale. Binding documentation to assertions
has a cost that scales with how much you have written, and this project writes a
lot — a system where changing your mind gets more expensive over time is the
ratchet this methodology exists to avoid.

Bind **invariants** ("these two scripts must compose identically"), never
**rationale** ("we chose X because Y"). Rationale ages, and freezing it prevents
the revision this section is arguing for. Prefer a report that surfaces drift
over a check that blocks on it, and reserve the blocking form for the few claims
where being wrong is both silent and expensive.

And let bindings expire. When the thing a check protects is retired, the check
goes with it — normally, without an argument that it was wrong.
