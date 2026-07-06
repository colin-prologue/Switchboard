# Constraints to Establish — Verify Before Designing

Facts the fresh session must resolve before committing to a v1 design. The
first three are load-bearing: the claim primitive (I-13) and the gate model
(I-3) cannot be designed without them. Resolve by inspection where possible
(probe the jira-mcp, read the board config), ask Colin otherwise. Record each
answer back into this file as it's established.

## 1. Board control (blocks the gate model)

- [ ] Is the shared board a **dedicated project** for this workflow, or the
      team's **existing board** with its existing statuses and human traffic?
- [ ] Can we get workflow-admin changes (new statuses/transitions,
      transition conditions) on Jira DC, and on what timeline?
- **Fallback if no workflow control:** overlay gate states on labels or a
  custom field, and treat native status as advisory. This is exactly the hack
  Switchboard used on GitHub — it works, but you lose transition-level
  enforcement (conditions/validators), pushing more of I-1 into the daemon
  and CI. Decide eyes-open, as an ADR.

## 2. jira-mcp capability surface (blocks the claim primitive)

Probe what the existing jira-mcp can actually do:

- [ ] Transition an issue through a workflow transition (not just edit fields)?
- [ ] Set/clear assignee?
- [ ] Read-after-write reliably (fetch fresh issue state immediately after a
      mutation, no stale cache)?
- [ ] JQL query by status/label/assignee/updated-since (the dispatch query)?
- [ ] Read and create issue links (blocks / is-blocked-by) — the dependency
      edges the scheduler gates on?
- [ ] Comment create/read?
- [ ] Anything resembling conditional update / optimistic concurrency?
- **If the mcp is too thin:** Jira DC REST v2 direct is the fallback; the mcp
  pathway is preferred (already approved/authenticated), not mandatory.

## 3. Claim-loss recovery policy (blocks I-15)

- [ ] What happens when a daemon's machine sleeps mid-session? Decide the
      liveness mechanism: lease with TTL stamped on the ticket, heartbeat
      comment/field, or manual steal with a protocol. Each has failure modes
      when the owner wakes up again — design against I-9/I-14.
- [ ] What is the reconciliation story for a half-done workspace on a machine
      that never comes back? (Branch pushed? Claim released with a comment
      pointing at the branch?)

## 4. Enforcement surfaces (blocks I-1 placement)

- [ ] Are Jira DC **webhooks / Automation** available to us, or admin-gated
      out of reach? (Determines whether board-side rules can be enforced
      server-side or only daemon-side.)
- [ ] GitHub: which instance (Enterprise Server / EMU / .com org)? Confirm
      branch protection, required approvals, CODEOWNERS availability, and
      whether required-approval counts distinguish author vs. approver the
      way Gate C needs.
- [ ] CI: what runs, and can we add required checks (the July-3 lesson says
      the red-main class of failure is prevented here)?

## 5. Organizational

- [ ] Claude usage policy at L&W: what may agents read/write? Any code or
      data that must not enter model context? (Gambling industry — assume
      compliance constraints exist until shown otherwise.)
- [ ] Rate limits / IT posture on Jira DC and GitHub for N daemons polling.
      Polling cadence × N engineers is a different load profile than one
      operator.
- [ ] Team size N for v1, and who besides engineers touches the board
      (PMs, QA?) — non-participant writes to shared tickets are a
      reconciliation source (I-14), not an error.
