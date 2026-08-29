# AgDR-2026-08-29 — Retire the legacy top-level `claude:` block

**Status:** accepted (2026-08-29)
**Surfaces:** `orchestrator/src/orchestrator/workflow.py`, `spec/SPEC.md` §1,
`workflow/WORKFLOW.base.md`, `workflow/stances/WORKFLOW.prototype.md`,
`projects/switchboard-self/WORKFLOW.md`
**Amends:** `AgDR-017`

## Context

`AgDR-017` added the `providers:` envelope beside the existing top-level
`claude:` block and read both, comparing them as typed values so neither could
silently win. It named its own weakest point: without a later removal criterion
the compatibility code becomes permanent, and the shipped template never proves
the envelope in production.

Both halves came true. No criterion was set. The `codex-canary` and
`mixed-canary` bindings — the planned vehicle for exercising the envelope in
production — were retired on 2026-08-15 as dormant, which was right on its own
terms and closed that route. The layer was permanent by default, not by
decision.

Issue #159 framed the choice as three shapes: pick a date, pick a condition, or
accept permanence. The framing was backwards, and noticing that is what settled
it. At `944af20`:

- `workflow.py:711` — `codex()`: *"Codex has no legacy top-level form."*
- `workflow.py:797-803` — `mixed()` raises `unsupported_provider_id`: *"mixed
  mode does not accept legacy execution blocks."*

The legacy block cannot express Codex, and mixed mode refuses any binding
carrying one. The compatibility layer was not protecting multi-provider work; it
was the thing preventing multi-provider from running on any production binding.
"Accept permanence" was therefore not the cheap option — it was the option that
permanently blocked the envelope on every real binding.

The inventory at `944af20` also made the condition immediately satisfiable:
three tracked bindings on the legacy form (`workflow/WORKFLOW.base.md:102`,
`workflow/stances/WORKFLOW.prototype.md:106`,
`projects/switchboard-self/WORKFLOW.md:102`) and one on the envelope
(`projects/switchboard-self/WORKFLOW.pilot-codex.md:51-61`).

## Decision

Set the removal criterion as a **condition** — "the dual-read is removed when no
tracked binding uses the legacy top-level form" — and satisfy it in the same
change rather than scheduling it.

1. Migrate all three tracked legacy bindings to `providers.claude` with
   `kind: claude-cli`. Mechanically a re-indent plus one line each; every field
   the legacy blocks carried is in the envelope's allowed set, so no setting's
   parsed value changes.
2. `Config.claude()` no longer reads the legacy shape. A top-level `claude:`
   key now raises `unsupported_provider_id` with a message naming the migration,
   instead of being parsed. The `conflicting_provider_config` comparison is
   deleted with the branch it guarded — two forms can no longer both be present.
3. `_parse_claude` becomes unconditionally strict. Its `strict=False` mode
   existed solely to preserve the legacy block's coercions (a non-string command
   and a boolean budget falling back to defaults); with that block gone, the
   lenient branch is unreachable, so the flag goes rather than lingering as a
   parameter nobody may pass.
4. A workflow carrying **no** execution block still resolves to the documented
   defaults. That is the absence of configuration, not a second shape, and
   making the envelope mandatory is a separate change with its own blast radius.
5. `mixed()`'s legacy-block refusal is **kept**, with a comment recording that
   it is unreachable by construction rather than dead. Its test is kept too.

The judgment call inside this decision is (4): "remove the dual-read" could be
read as "require `providers.claude` everywhere". It is deliberately read
narrowly, as removing the second *shape*. See Weakest point.

## Rejected options (steelmanned)

- **A date or version.** Simplest to write and easy to schedule against. Rejected
  because it is arbitrary and gets renegotiated the first time it is
  inconvenient — and `AgDR-017`'s prediction is precisely that a
  removal-someday note produces nothing.
- **Accept permanence, delete the "temporary" framing.** Honest, and cheaper than
  a migration nobody needs. It deserved real consideration and lost on evidence,
  not on principle: the cost it accepts is not "the envelope goes unproven" but
  "no production binding can ever run a second provider", because `mixed()`
  refuses a legacy block outright. That is not a theoretical cost deferred, it is
  a capability foreclosed.
- **Migrate the bindings, keep the dual-read.** Zero risk to any unknown consumer,
  and the parser change could follow later. Rejected because it leaves exactly
  the artifact this project keeps getting bitten by: a compatibility path with no
  remaining user and no failing check, which the next reader assumes is load-
  bearing. The criterion's whole point was to name the moment it stops being one.
- **Also require `providers.claude` (no defaults path).** Strictly one load path,
  and symmetric with `codex()`. Rejected as scope: an execution block has always
  been optional, that is a spec-level property independent of which shape carries
  it, and requiring it would change what a minimal workflow does at load time for
  reasons unrelated to #159.
- **Delete `mixed()`'s legacy-block guard as now-dead code.** Tempting once no
  binding can reach it. Rejected because it states mixed mode's own rule at mixed
  mode's own boundary; a future provider that reintroduces a top-level block
  would land on it first, and removing a guard because the current caller
  happens not to trigger it is how guards get lost.

## Blast radius

Startup/reload only — a config-shape change, not a runtime one. A workflow still
carrying a top-level `claude:` block now fails to load with a named error where
it previously loaded; that is a startup-refusal path, and the existing
invalid-reload behaviour (retain last-known-good, block new dispatch) already
covers it. Every consumer of the Claude config view — the runner, the
scheduler's timeout and budget policy, `validate_dispatch` — sees identical
values for all three migrated bindings, because the envelope's parse of those
fields is the same parse.

The three templates and the composed `projects/switchboard-self/WORKFLOW.md` are
migrated in one commit so the `scripts/verify-setup.sh:143-144` recomposition
drift check stays green.

Unchanged: Codex support, `routing.weights`, `MixedRunnerSelector`, the provider
circuit, `AgDR-046`'s typed provider codes, and the retired canaries (not
revived — if the envelope needs production exercise that is a new decision with
a new vehicle). `AgDR-018`'s provider-neutral policy views remain a separate,
unmet, currently unowned obligation; this change does not resolve it.

## Weakest point

The migration edits every tracked legacy binding in one commit, and the canary
bindings that used to exercise config changes were retired on 2026-08-15 — so
there is nothing left running the old path to notice a divergence. The claim
that the envelope parses these three bindings identically rests on the field
sets being equal and both paths ending in the same `ClaudeConfig`, checked by
reading rather than by a differential run. If any one of them parses differently,
all three change behaviour at once and the first evidence is production.

Second: (4) leaves a workflow with no `providers:` key resolving to defaults, so
a misspelled `providers:` still yields a default `claude -p` command rather than
a load error. That was equally true before this change, so it is not a
regression — but this record is the reason it is now the *only* silent path
left, and if it ever fires, the fix is to make the envelope mandatory rather
than to reopen the dual-read.
