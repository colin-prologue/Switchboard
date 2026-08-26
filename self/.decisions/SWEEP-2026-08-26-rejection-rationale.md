# Sweep 2026-08-26 — rejection rationale

**Scope:** all 29 records carrying a `Rejected options` section (~120 rejections).
**Question asked of each:** *is the rejection's rationale still **relevant and
meaningful**?* — not "is it still true".
**Bar for retiring one:** a named artifact that changed. An argument is not
admissible; a commit, capture, fixture, deleted subsystem, or stated operating
constraint is.

This is the companion to the `Weakest point` sweep in `methodology/METHODOLOGY.md`.
That one re-reads predictions. This one re-reads rejections, which had no reader.

## Why the wording matters

Four records (`AgDR-020/021/022/024`) hold **17 rejections** about running the
Codex canaries. PR #155 deleted those bindings on 2026-08-15. Under *"is Y still
true?"* nearly all survive — the claims about Codex sandboxing, the `python`
alias and weighted selection remain accurate. Under *"is Y still relevant?"*
they retire: they constrain a subsystem that no longer exists. The truth test
would have preserved 17 pieces of dead weight a future reader would treat as
binding.

## Headline finding

`AgDR-030` (2026-08-09), rejecting an option on the **Codex** adapter:

> **Treat EOF-after-error as `port_exit`.** Rejected because it discards the
> classification the CLI handed us and would collapse a real provider auth
> outage into `RUNNER_PROTOCOL`, which AgDR-026 excludes from circuit triggers —
> reopening the original bug from the other end.

That is exactly the defect found in round 7 of PR #166, in the **Claude** runner,
and fixed on 2026-08-26. The hazard was identified in writing three weeks
earlier, named `RUNNER_PROTOCOL` and `AgDR-026` explicitly, and predicted the
consequence precisely. It sat in a rejection on the other provider's record and
nothing carried it across — the *second-reader problem* METHODOLOGY.md describes,
in its purest form.

## Category A — Violated: rationale still correct, the code did it anyway

| Record | Rejection | Violation |
|---|---|---|
| `AgDR-026` | "Park every affected issue" — *an outage must not consume issue allowance* | civ-life #8, five sessions, parked at 5/5. **Now pinned** by `test_a_provider_outage_never_consumes_issue_allowance`. |
| `AgDR-016` | "Treat subscription limits as ordinary retries" — same invariant, stated independently | Same violation |
| `AgDR-025` | "Use exception or log-message strings as policy" | `_TEXT_PATTERNS` is exactly that; #165 was a wording it did not match |
| `AgDR-030` | "Treat EOF-after-error as `port_exit`" | The Claude runner did this until PR #166 |

**The same constraint is written in three separate records' rejection sections
and was violated anyway.** Nobody treated these as immutable; nobody treated them
as anything. This is the sweep's central result, and it reframes the problem: the
project's real constraints are frequently recorded in the one section with no
reader, no teeth, and no cross-record propagation.

## Category B — Decayed: rationale no longer relevant

- **`AgDR-013` — acted on in this sweep.** It rejected "keep 20 turns, add
  `--resume`" as *"the correct structural fix … ticketed separately rather than
  rushed"*, and raised `claude.max_turns` 20 → 100 as a stopgap. Issue #47 /
  `AgDR-027` shipped resume on 2026-07-27. `max_turns` was still 100 seven weeks
  later, and the config comment still read *"Structural fix … is ticketed
  separately"* in the present tense. Restored to 20.
- **`AgDR-017`** — "Replace `claude:` immediately" was rejected as *"a flag-day
  change across every registered project … while execution boundaries are
  changing"*. Two projects are registered and the boundaries stopped moving when
  #155 retired the canaries. This is the answer open issue **#159** is asking for.
- **`AgDR-016`** — "Require API-key billing" was rejected on canary scope, and
  names its own revisit condition: *"remains the likely production path if
  subscription limits are operationally constraining."* Claude OAuth lapsed
  mid-wave in under 24h (civ-life #8 transcripts, 2026-08-17/18/19). The
  condition has arguably fired.
- **`AgDR-018` / `AgDR-019`** — rejections premised on "only one valid provider"
  and "scheduler policy still reads Claude config". Mixed routing shipped
  (`AgDR-023`). Expired, low current relevance while Codex is unused.
- **17 canary rejections** (`AgDR-020/021/022/024`) — subsystem deleted by #155.

## Category C — Superseded silently

`AgDR-025` rejected "post issue comments for every provider failure" (*"tracker
noise … exposes infrastructure conditions on work items"*). `AgDR-046` now does
it, having defeated both clauses — edge-triggered per circuit generation, and
posted to a dedicated ops issue rather than the work item. Correct supersession,
never recorded. *Easy to supersede, hard to silently contradict* — this was the
silent kind.

## Intact — no action

`AgDR-010`, `023`, `027`, `028`, `029`, `034`, `035`, `037`, `042`, `044`, `045`.
Worth naming explicitly so the sweep is not read as a list of grievances.
`AgDR-042`'s pidfile rejection is a good example: it rejected a pidfile *as the
lock* while `singleton.py` keeps a PID protocol *for querying* — the distinction
held, and the stale civ-life lock on 2026-08-25 was diagnosed through exactly
that protocol.

`AgDR-036` names a revisit condition (*"worth revisiting if the deny-list proves
load-bearing"*) that has arguably fired — it is load-bearing on Claude and absent
on Codex (#135) — but Codex is unused, so no action now.

## Not audited

`AgDR-046` was written hours before this sweep by the same author. Its rejections
need a reader who is not me.

## What follows

1. `claude.max_turns` restored to 20 — done here.
2. The four Category-A records amended in place with dated headers.
3. A methodology amendment: sweep rejections as well as predictions; ask
   relevance rather than truth; require a named artifact to retire one.
