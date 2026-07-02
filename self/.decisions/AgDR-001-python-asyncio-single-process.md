# AgDR-001: Python + asyncio, single process per project

- **Status:** accepted (autonomous run, 2026-07-01)
- **Context:** SETUP.md Stage 3 left the orchestrator language open
  (TypeScript example vs Python). Colin chose Python at the setup gate. Within
  Python, the concurrency model was the agent's call.
- **Decision:** One asyncio event loop per project process. Workers are tasks;
  the agent subprocess is `asyncio.create_subprocess_exec`; retry timers are
  `loop.call_later`. All scheduling state is mutated only on the loop
  (core §7.4 single-authority rule falls out for free).
- **Forces:** core spec's "N processes, one per project" topology; no shared
  state across projects; subprocess-heavy workload (claude, hooks) is I/O bound.
- **Weakest point:** asyncio subprocess + process-group kill semantics are the
  fiddliest part of the codebase; a threaded design would have simpler kill
  paths but a much harder single-authority story.
- **Deps:** pyyaml, httpx, python-liquid — nothing else.
