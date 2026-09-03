# AgDR-2026-09-03-the-inbox-digest-is-a-snapshot-not-a-feed

## Context

On 2026-08-29 the launchd-run fleet swept the entire active queue — ten tickets
became PRs #182–#191, plus one fail-review episode on #16 — and the operator
learned of it six hours later, by grepping launchd logs. Ten PRs sat unnoticed.

That is not an anomaly. `AgDR-048` declares Switchboard a single-operator system
whose control surface is session-mediated signals, and concedes in its own
weakest-point section that until #12 lands "the only running-state surface is
`gh issue list` plus launchd logs". The system has exactly one operator-facing
push channel — the ops log from `AgDR-046` — and it carries one kind of event:
everything stopped. Nothing carried the routine case, which is the case that
actually recurs.

Issue #192 asked for a digest. What it did not settle, and what this record
settles, is the *write mode* — and that turns out to be the whole design.

## Decision

**The digest is one always-current body, rewritten in place. It is never a
comment, and never a feed.**

Concretely:

1. **Its surface is a dedicated find-or-create issue** ("Switchboard operator
   inbox"), resolved single-flight under its own lock and revalidated before a
   cached id is reused — the ops-log mechanism, second instance. It carries
   **no `status:*` label**, which is what structurally forecloses the
   dispatcher ever claiming it.
2. **The write is read-compare-write over `update_issue_body`** — the fold-apply
   mechanism, second instance. Unchanged content produces **no write at all**; a
   body changed under the read is refused and retried next cycle; the write is
   verified after it lands.
3. **The feature writes no comment and no label on any tracked issue, ever.**
   This is an invariant with a test, not a property of the current code path.
4. **The "since last digest" watermark lives in the digest body**, above the
   compared region, and advances only on a verified write.
5. **The compared region carries no timestamp.** Everything that moves per
   render — the freshness stamp, the window start, the next watermark — lives
   above a sentinel and is excluded from the compare.
6. **It runs in-tick**, last, after dispatch, and every failure is non-fatal.

Point 5 is the one that looks cosmetic and is not. It is what makes points 2 and
6 compatible: a single timestamp inside the compared content would force a write
on every cycle, and a report that writes every cycle on the poll loop is the
noise surface `board_sanity` explicitly refuses to be.

## Rejected options, steelmanned

**Post a recurring comment on the digest issue.** The strongest version: a feed
is *append-only*, so it can never lose history, an operator can scroll back
through what was true last Tuesday, and GitHub's own notification machinery does
the pushing for free — which is more than a silently-edited body gets. That last
point is real and this decision pays for it: a body edit notifies nobody.

Rejected because the history a feed preserves is history nobody asked for, and
the cost is a channel this project has already been burned by. OBS-022
(2026-07-02): a park notification comment bumped the issue's `updatedAt`, which
was the unpark signal, producing an unbounded park→comment→unpark spend loop —
the exact failure the cap existed to prevent — which survived 110 passing tests
because the fake did not model GitHub's comment→`updatedAt` echo. A digest
commenting *near* tracked issues re-enters that incident class outright; a
digest commenting on its own issue merely grows an unbounded thread whose Nth
entry is the only one anyone reads. The operator wants "what awaits me *now*",
and every superseded entry in a feed is a wrong answer to that question that
looks like a right one.

**Make it a section of the existing ops log.** One issue, one channel, one
find-or-create, no second surface to explain. Genuinely leaner on the axis of
"how many things exist".

Rejected because the two have incompatible write modes and the incompatibility
is load-bearing in opposite directions. Latch notices are append-only *by
design*: a stopped system's only signal must not be overwritable. The digest is
whole-body replacement by design. Sharing one issue means either the digest
appends (rejected above) or the digest's body rewrite sits above a comment
stream it must never touch — a surface where one bug in the wrong direction
silently deletes the "everything stopped" notice. Two issues cost one extra
find-or-create and buy a write mode that cannot reach the outage channel.

**Run it as an external observer rather than in-tick.** PHI-036 (2026-06-15)
argues that metered resources should be observed out-of-band because the
reporter must not be the thing that fails — and an external observer still
reports when the fleet is dead or capped, which an in-tick digest by
construction cannot.

Rejected, and the ticket's own triage reached the same answer on 2026-09-03. A
dead fleet accumulates nothing *new* for the inbox; the last-written body stays
readable; and "the reporter died" is precisely the health ticket's mandate. A
second always-on component here would duplicate that ticket for one marginal
state it already owns. PHI-036's own known-tension clause covers the case: the
resource being observed — operator-facing backlog — only grows while the fleet
runs.

**Ship it disabled by default,** the way `FoldConfig` and `ReviewResponseConfig`
ship. Consistent, and conservative about a feature that writes to the tracker.

Rejected because the analogy does not hold. Those two gate on an identity the
orchestrator cannot invent (which login is the operator? which is the bot?), so
shipping them off is the only honest default. The digest needs no identity: it
enumerates what is already public on the board and writes to an issue it creates
itself. A digest shipped off would be the exact shipped-but-unwired shape issue
#172 exists to *report on*, landed on the one feature whose entire purpose is to
stop things going unnoticed.

## Blast radius

- **Two new tracker surfaces.** One read (`fetch_open_prs_repo_wide` — reads
  were never the §11.5 restriction) and one write path reusing two already-
  sanctioned mutations (`createIssue`, `updateIssue(body)`). Recorded in
  SPEC.md §4.
- **The ops-log find-or-create was generalized** to a shared title-matched
  helper. Behaviour for the ops log is unchanged; the seed body and title are
  now parameters.
- **`_park`'s comment prefix moved into the digest module** and is imported by
  the writer. Backwards-looking, and the only direction with no import cycle —
  the scheduler already depends on the digest module. One literal, one source:
  the park comment is the only durable record of *why* an issue is parked, so a
  second spelling of it would silently empty the digest's park-reason column.
- **On the poll loop, cadence-gated.** Default one digest per day against a
  ~30s tick, stamped at exec, so a slow or failing digest consumes its interval
  rather than re-running every tick.
- **Not a consumer of anything it reports on.** A digest run leaves every
  tracked issue's `updatedAt`, labels, comments, and body untouched. The unpark
  machinery and the fold loop are explicitly *not* downstream of it.

## Weakest point

**The compared-region discipline is a convention, and conventions drift.**

The no-write-when-unchanged property — which is what licenses running this on
the poll loop at all — rests entirely on nothing time-varying appearing below
`CONTENT_SENTINEL`. Today one test asserts it, by rendering the same `Inbox`
twice six hours apart and comparing the extracted content. That test survives
only as long as someone reads it before adding "last checked: {now}" to a
section header, which is the single most natural thing a future author will
want to add. The failure is silent in the worst way: it does not break anything
visible, it just makes the digest write on every tick, and the first symptom is
an issue with ten thousand body revisions and an API budget nobody expected.

**What would make this wrong:** if the digest issue's body revisions grow at
roughly the tick rate rather than the cadence rate, the discipline has already
broken and the test did not catch it. That is checkable directly — the body
write count is observable — and it is the condition to sweep for.

A second, smaller one: the digest notifies nobody. It is correct on GitHub, and
it is still a pull surface — an operator who does not look at the repo does not
see it. This decision accepts that because the ticket's premise is "where the
operator already looks", and the same premise is what the ops-issue mechanism
already bet on. If that premise turns out false, the fix is a push channel, not
a different write mode, and this record should be superseded rather than
patched.

## References

- Issue #192 (this record's ticket), issue #12 (web dashboard; the non-goal
  boundary), issue #172 (shipped-but-unwired reporting).
- `AgDR-046` (the ops issue: find-or-create, single-flight, accepted create
  residual), `AgDR-048` (single-operator declaration and its weakest point),
  `AgDR-008` (durable parking; the label carries the fact, the comment the
  reason), `AgDR-045` (gates are declared per project), `AgDR-047` (the
  fail-review verdict's grep-able anchor), issue #126's fold-apply
  read-compare-write precedent, issue #52's `board_sanity` non-fatal posture.
- Oracle bank: OBS-022 (2026-07-02, comment→`updatedAt` self-unpark),
  OBS-023 (fake fidelity), PHI-036 (2026-06-15, out-of-band observation and its
  known-tension clause).
