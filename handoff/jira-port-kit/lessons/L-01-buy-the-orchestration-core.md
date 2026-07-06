# L-01 — Buy the orchestration core, own the bindings

**Context.** Switchboard v1 tried to be a framework. It collapsed under
self-built orchestration (see L-10). The rebuild vendored OpenAI Symphony's
orchestration spec (a one-time copy, then owned — not a fork or dependency)
and put all originality into the bindings: the methodology, the tracker
adapter, the executor adapter.

**Lesson.** Orchestration — polling, dispatch, retries with backoff,
workspace lifecycle, reconciliation, subprocess management, observability —
is a solved problem, and every bug in it is a concurrency bug. Spend your
novelty budget on the parts that are actually yours: the ticket protocol, the
gate methodology, the Jira and Claude bindings. The dependency arrow points
up: substrate below, methodology as configuration on top, so the methodology
survives substrate revisions.

**Portable.** The split itself: scheduler core vs. tracker adapter vs.
executor adapter vs. methodology-as-config. Whatever you build, keep the
tracker (Jira) behind an adapter boundary that the scheduler core never
reaches around.

**Not portable.** The specific choice of Symphony, Python/asyncio, and the
vendor-a-spec maneuver. Evaluate fresh — including whether a much smaller
core suffices for v1.
