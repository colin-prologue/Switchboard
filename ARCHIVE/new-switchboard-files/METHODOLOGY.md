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
| `status:todo`          | **yes** | Approved, unblocked, dispatchable                              |
| `status:in-progress`   | **yes** | An agent is working it                                          |
| `status:plan-review`   | no      | Gate B handoff — agent produced a plan/ADR awaiting approval    |
| `status:human-review`  | no      | Gate C handoff — implementation done, awaiting human merge      |
| `status:blocked`       | no      | Parked (fallback when native dependencies aren't available)     |
| *(issue closed)*       | —       | Terminal                                                       |

Dependencies use GitHub's native **blocked-by**; Symphony won't dispatch a
`status:todo` issue while any blocker is unresolved.

## Gates

- **Gate A — intent/spec approved.** A ticket sits at `status:drafting` until a
  human approves its task-intent and acceptance criteria, then moves it to
  `status:todo`. The agent never sees an unapproved ticket.
- **Gate B — plan/architecture approved.** For architecture-touching work, the
  agent produces an implementation plan + ADR, parks at `status:plan-review`, and
  a human approves before child tickets are filed.
- **Gate C — final review.** Every implementation hands off at
  `status:human-review`. A human merges. Agents never self-merge.

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

- a one-paragraph **intent** (what + why),
- **acceptance criteria** written as checks (pass/fail, eval-shaped),
- **non-goals** (hard scope boundaries),
- **assumptions** (things taken as given; if false, the ticket is void),
- a `parent-intent: <slug>` line if it inherits a product-intent file.

Acceptance criteria are the agent's definition of done; non-goals are boundaries
it must not cross. (Product-intent files, the verification contract, and the
elicitation front door arrive in later roadmap phases.)
