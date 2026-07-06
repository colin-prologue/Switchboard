# Jira Port Kit — Switchboard Principles for Light & Wonder

This kit distills what was learned building and operating **Switchboard** (a
GitHub-Issues-backed orchestration system for autonomous Claude coding agents,
run by a single operator) so a fresh session can build a **v1 of a Jira-backed
equivalent** at Light & Wonder — run by **any number of engineers on their own
machines against one shared Jira board**.

It is deliberately NOT a design. The mechanics of Switchboard (Python/asyncio,
GitHub labels, its exact label taxonomy, its vendored Symphony spec) are
explicitly non-portable. What carries over is:

- **CONSTITUTION.md** — invariants that BIND you. Violating one of these
  reproduces a failure we already paid for.
- **lessons/** — hard-won learnings that INFORM you. Each says what happened,
  what the lesson is, and which parts are portable vs. Switchboard-specific.
- **TARGET-CONTEXT.md** — what is already known and decided about the L&W
  environment. Treat as ground truth unless Colin revises it.
- **CONSTRAINTS-TO-ESTABLISH.md** — facts you must verify before committing to
  a design. Several are load-bearing; do not design past them on assumption.

## Consumption order (for the fresh session)

1. Read `CONSTITUTION.md` in full. These are requirements, not suggestions.
2. Read `TARGET-CONTEXT.md` for the decided facts.
3. Read every file in `lessons/` (each is under a page).
4. Work through `CONSTRAINTS-TO-ESTABLISH.md` — resolve what you can by
   inspection (e.g., probe the jira-mcp capability surface), ask Colin for
   what you cannot.
5. Only then design v1. Bring a fresh perspective: challenge any Switchboard
   mechanism that isn't in the constitution. The point of re-implementing
   rather than porting is that you may find better shapes for Jira and for
   multi-operator than Switchboard's single-operator shapes.

## Kickoff prompt (Colin: paste this to start the fresh session)

> You are building v1 of a system that lets any number of engineers on my team
> run autonomous Claude coding agents from their own machines, coordinated
> through our shared Jira board (self-hosted Jira Data Center, reached via our
> existing jira-mcp), with code on GitHub. A kit of principles and hard
> learnings from a predecessor system is in `jira-port-kit/` — read its
> README and follow the consumption order before proposing anything. The
> constitution binds; the lessons inform; the predecessor's mechanics are
> explicitly not to be copied. Operating model for v1: engineers practice the
> ticket protocol interactively AND run per-engineer daemons — both ship in
> v1. Start by resolving the constraints checklist, then brainstorm the
> design with me.

## What v1 must contain (scope anchors)

- The **shared ticket protocol**: what a well-formed ticket looks like, the
  gate states it moves through, and who/what may move it. Engineers follow it
  interactively; daemons follow it autonomously. Same protocol, one board.
- The **per-engineer daemon/orchestrator**: polls the shared board, claims
  eligible tickets atomically, dispatches Claude sessions in isolated
  workspaces, respects caps, parks with visibility. Multiple daemons must
  coexist without stepping on each other — that property is designed in from
  the start, not added later (see constitution).
- The **review gate**: agents never merge; a human other than the effective
  author approves (peer review — the team gives you this for free, unlike the
  solo predecessor).
