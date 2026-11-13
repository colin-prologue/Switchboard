# AgDR-2026-11-13-dispatcher-retired

## Context

Switchboard dispatched its last end-to-end merge on 2026-09-13 and its last
bot activity was 2026-10-07. A cross-project audit in November 2026 found the
owner's limit was review time, not queue depth: every one of the 68 pull
requests Switchboard produced was still merged by hand, and about 38% of the
owner's turns across projects were approvals or relays.

What the queue held on 2026-11-13:

- civ-life: three issues at `status:human-review`, each with a finished,
  mergeable pull request (#64, #67, #71). They wait on a human merge, not on
  the dispatcher.
- Switchboard: 18 issues (16 `status:drafting`, #214 `status:decision`, #220
  `status:todo`). All are improvements to Switchboard itself.

Only two projects were ever registered (`civ-life`, `switchboard-self`).
Nothing outside this repo invokes the orchestrator.

Outside evidence pointed the same way. Practitioners working solo with coding
agents report that review caps them at about one significant change at a time,
and several who built custom orchestration layers removed them once the agent
tools shipped the same features natively.

## Decision

**The dispatcher is retired. No machine runs the orchestrator, and no new
project is registered.** Work is started in native agent sessions; plain
GitHub issues, without the `status:*` state machine, serve as memory between
sessions.

Three ideas are kept and now live outside this repo, in the owner's global
agent instructions:

1. **Risk decides review.** How much human review a change gets depends on
   what it could break (the stance idea), not on a fixed gate.
2. **An explicit escalation list.** Agents stop for a person only on named
   conditions and otherwise decide, record one line, and continue.
3. **Decision records.** Load-bearing choices are written down where the
   owner will see them.

This repository stays as a reference. The 18 open Switchboard issues are left
as they are; they describe improvements to a system that no longer runs.

## Rejected alternative (steelman)

**Keep Switchboard as the backbone.** It is the only thing that gives an
unattended overnight queue across projects with retry limits, parking, a
handoff check and a digest; nothing native replaces all of that today, and
15.8k lines of working, tested code would keep their value. Rejected because
the queue was never the constraint, the logs show it needed regular rescue
(about 100 timeouts and 54 session-cap hits in under three weeks, and one
incident where ten pull requests went unnoticed for six hours), and a month
idle produced no missed work.

## Blast radius

- Reversible: the code, decision records and issue history are untouched.
  Restarting means re-enabling the service on one machine
  (see AgDR-2026-09-13-one-machine-owns-dispatch-per-project).
- Left behind: the `switchboard-agent` GitHub App and its local key, the
  per-issue workspaces on each machine, and `status:*` labels on civ-life
  and mini-fax issues (mini-fax only ever borrowed the labels).
- Not done by this record: stopping any service still installed on the WSL
  desktop. That is a manual step on that machine.

## Revisit when

Agents merge low-risk work without a person and the owner wants an unattended
queue again, and the native tools still do not provide one.
