# AgDR-003: WORKFLOW.md reload via per-tick mtime polling

- **Status:** accepted (autonomous run, 2026-07-01)
- **Context:** Core §6.2 REQUIRES detecting WORKFLOW.md changes and
  re-applying config without restart. Options: fs-watcher dependency
  (watchdog/watchfiles) vs polling mtime.
- **Decision:** stat the file at every poll tick (and keep last-known-good on
  invalid reload). Detection latency is bounded by `polling.interval_ms`
  (default 30s), which is the same cadence config changes take effect anyway.
  Zero extra dependencies; also satisfies §6.2's "re-validate defensively in
  case watch events are missed" clause by construction.
- **Weakest point:** a change made and reverted within one tick window is
  invisible — harmless for this use.
