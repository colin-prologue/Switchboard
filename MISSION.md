# Switchboard — Mission

## Mission

Switchboard exists to let one person direct many AI agents across long-lived codebases
without losing the thread. It routes each piece of work to the cheapest model that can do it
well, captures the reasoning behind every decision as durable memory, and keeps human
judgment at the moments that actually matter. The aim is leverage with accountability: more
work done autonomously, with the intent behind it preserved and auditable for years.

## The problem it answers

Agentic coding today is powerful but forgetful and expensive. Plans live in one session's
context and evaporate; the *why* behind an architecture is lost the moment the chat closes;
every step risks burning a frontier model on work a cheap one could do; and the human is
either a bottleneck on every turn or absent when a real decision is made. On a codebase
maintained over years, this compounds into systems no one understands and choices no one can
explain. Switchboard is the answer to "how do I run a lot of agents, cheaply, and still know
in three years why we built it this way."

## Principles (what every decision is tested against)

- **Cheapest capable model.** Reserve the top tier for decisions that compound; push
  mechanical work down. Spend where capability pays off, nowhere else.
- **Oversight at the seams.** Humans review at phase gates, not inside phases. Meaningful
  checkpoints, not constant supervision.
- **Intent is a first-class artifact.** Capture *why*, not just *what* — alternatives weighed,
  constraints, evidence — and preserve it for whoever comes next.
- **Ground in precedent.** New decisions reason from this team's own history, not from
  scratch. The log is read, not just written.
- **Local and legible.** Coordinate through plain files in git. No services to trust, no
  infrastructure to stand up, full audit trail for free.
- **Decentralized memory, shared engine.** State and decisions live with each project; the
  logic is installed once and versioned. Per-project isolation suits regulated work.
- **Loose where it evolves, strict where it's relied on.** Versioned contracts with
  deliberate expansion joints — disciplined structure, room to grow.
- **Honest over flattering.** The system should surface real problems and real tradeoffs.
  Validation theater is a failure mode, not a feature.
- **Don't reinvent solved problems.**

## What it is, in one breath

A goal goes in the front door; it's grounded in past decisions and decomposed into a plan
with tiered routing and phase gates; tier-pinned workers run each task in a fresh, isolated
session — protected from spirals, cheap on context — and write their results and decisions
back; humans review at the gates, and that feedback becomes part of the permanent record;
the next goal grounds in everything learned. Above many such projects sits a deferred nexus:
a single console to see progress, set goals, and give feedback across all of them.

## Where we are

The single-project design is settled and a flat reference implementation runs end to end
(validated: plan → tiered queue → gated execution → decision capture → human sign-off →
unblock). The active work is restructuring it into an installable plugin — a shared engine
plus a per-project instance. The multi-project nexus is designed in principle and parked.

## Where to push (scrutinize these bets)

- Is **decentralized state + shared engine** right at multi-project scale and under
  regulated constraints, versus a central hub?
- Does **git-as-coordination** hold up, or break before the worker pool is large enough to be
  useful?
- Is **a fresh session per task** genuinely better than a persistent context with compaction,
  at real workloads and cost?
- Are **phase gates** the right granularity for human oversight — too coarse, too fine?
- Will the **decision log actually get used** by future sessions, or quietly become
  write-only?
- Is **deferring the nexus** correct, or does the multi-project control plane need to shape
  the single-project design now, before it's expensive to change?
