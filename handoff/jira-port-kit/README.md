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

## Transport (Colin: moving this to the L&W machine)

The kit is self-contained: every cross-reference resolves inside this folder,
and nothing in it links back to the Switchboard repo. Copy **only this
folder** — the predecessor's source, specs, and decision records deliberately
do not cross the boundary; everything portable from them is already distilled
here.

```bash
# from the Switchboard repo root, after the PR is merged:
git archive main handoff/jira-port-kit | tar -x
# then move handoff/jira-port-kit/ to the L&W machine (USB, scp, however
# files legitimately cross that boundary for you)
```

On the L&W side, drop the folder at the root of a **new, empty repo** that
will become the system's home, commit it as the first commit, and start the
fresh session there. Keeping the kit in that repo's history (rather than
pasting it into a prompt) is deliberate: it stays reviewable by teammates and
becomes the provenance record for every ADR the new session writes against it.

## Prerequisites for the fresh session (verify before kickoff)

The kickoff assumes the session can actually reach the three surfaces it
must work against. Confirm on the L&W machine:

- **Claude Code** (or equivalent harness) running, with enough permission to
  read/write the new repo.
- **jira-mcp connected and authenticated as you** — the session's first real
  task is probing its capability surface (CONSTRAINTS-TO-ESTABLISH §2), which
  requires a live connection to the actual board, not documentation.
- **GitHub access**: `gh` CLI authenticated (or equivalent), able to see the
  org, target repos, and branch-protection settings it must evaluate
  (CONSTRAINTS-TO-ESTABLISH §4).
- **A ticket sandbox**: one throwaway Jira issue (or a scratch project if you
  can get one) the session may mutate while probing transitions, assignee
  writes, and read-after-write behavior. Do not probe against live team
  tickets.

If any of these is missing, the session can still read the kit and draft the
design conversation, but constraint resolution — the gate before designing —
will stall.

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
