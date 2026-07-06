# L-08 — Safety is layered, and the unsolved parts are documented

**Context.** Switchboard's agent permission posture had three layers:
(1) CLI-level: auto-approve file edits, allowlist git/gh, everything else
falls to a non-interactive default deny; (2) a PreToolUse guard that hard-
denies file mutations outside the session's workspace; (3) directory
structure: the guard's settings file lives *beside* the workspace, never
inside it, so the agent can't commit its own leash.

**Two subtleties that mattered.**
- **Denials are soft.** The agent *sees* a denial and may route around it;
  only a session that cannot finish fails the attempt. This shipped
  deliberately — hard-fail on any denial made sessions brittle.
- **The scope limit is written down.** Bash commands are not statically
  analyzed — that's rabbit-hole territory. Blast radius is bounded instead
  by workspace cwd + fresh clone + allowlist. The guard trusts an env var an
  agent could theoretically bypass. These are *documented accepted risks*,
  reviewable later against actual behavior corpus, not silent gaps.

**Lesson.** No single layer is sound; the composition is. And writing down
what you are *not* defending against is as load-bearing as the defenses —
it converts unknown risk into a reviewable decision.

**L&W amplifier.** Blast radius is now N machines with N engineers'
credentials (operator identity), inside a gambling company's compliance
envelope. The layered posture and the documented-scope-limit discipline port
directly; the acceptable-risk lines almost certainly sit somewhere stricter.
Re-derive them explicitly (see CONSTRAINTS-TO-ESTABLISH §5) rather than
inheriting Switchboard's.
