# AgDR-017: Add a provider envelope through semantic dual-read

**Status:** accepted (2026-07-12)
**Surfaces:** `orchestrator/workflow.py`, `spec/SPEC.md`, workflow reload
validation, and the AI-agnostic agent-pool migration

## Context

Switchboard's execution settings live in one top-level `claude:` block. A mixed
pool needs named provider instances, but changing the schema in place would make
every existing project binding invalid at once. Keeping two uncoordinated
parsers would avoid that flag day but create a silent-precedence problem whenever
a generated or hand-edited workflow contains both forms.

Stage 2 changes parsing only. Claude remains the sole runtime adapter and the
scheduler still constructs `ClaudeRunner(cfg.claude())`. Codex configuration,
selection, and capacity do not exist yet.

## Decision

Introduce a `providers:` map whose keys are provider-instance ids. The only
accepted Stage 2 instance is `providers.claude`, and it must declare
`kind: claude-cli`. Its remaining fields and defaults are exactly those of the
legacy `claude:` block and resolve to the same `ClaudeConfig` dataclass.

Continue accepting legacy-only workflows unchanged. When both forms are
present, parse and default both independently, then compare the resulting typed
configs:

- equal typed values are accepted, including implicit-default versus
  explicit-default representations;
- unequal values fail with `conflicting_provider_config`, naming both paths;
- no textual or field-presence precedence rule exists.

If `providers` is present, malformed envelopes, a missing canonical `claude`
instance, unsupported ids, and unsupported kinds fail validation. A workflow
must not appear to enable Codex before a Codex adapter exists.

The provider form is strict even where the legacy parser historically coerces:
unknown fields, a non-string command, and boolean/non-numeric budget values fail
with a path-specific parse error. Legacy coercion remains unchanged. Textual
duplicate mapping keys are rejected by the loader before typed parsing, so
last-key-wins behavior cannot discard one side of a conflict. Duplicate
detection runs before SafeLoader expands YAML merge keys, preserving standard
`<<` inheritance and explicit override semantics for legacy workflows.

Do not convert `workflow/WORKFLOW.base.md` or composed project workflows in this
stage. Keeping them on `claude:` continuously tests the compatibility promise.
An invalid hot reload retains the last-known-good config for reconciliation and
blocks new dispatch through the existing reload failure path.

## Rejected options (steelmanned)

- **Replace `claude:` immediately.** One schema is simpler and removes migration
  logic. Rejected because it is a flag-day change across every registered
  project and makes rollback harder precisely while execution boundaries are
  changing.
- **New form silently wins.** This is common in config migrations and lets
  generated files carry both forms during rollout. Rejected because a stale
  legacy block would look harmless while operators and tools disagree about
  which budget, command, or timeout is active.
- **Legacy form silently wins.** This preserves production behavior but makes a
  newly added provider envelope appear active when it is not, which is more
  dangerous than rejecting the ambiguity.
- **Compare raw YAML maps.** Simple and strict, but rejects semantically equal
  configurations such as omitted `max_turns` versus explicit default `20`.
  Typed comparison matches runtime behavior rather than syntax.
- **Accept unknown providers now.** This permits forward-authored Codex config
  before Stage 4. Rejected because the scheduler would ignore it and still run
  Claude, turning a configuration error into an identity error.
- **Keep SafeLoader's duplicate-key behavior.** Standard PyYAML compatibility
  is attractive, but last-key-wins can erase a conflicting block before the
  dual-read comparison sees it. Rejecting duplicates makes ambiguity visible at
  the workflow boundary while retaining merge-key overrides.

## Blast radius

Additive for every legacy-only workflow: the same parser defaults and
`ClaudeRunner` construction remain active. New provider-enveloped workflows can
start using the alternate form, and malformed/ambiguous new configurations now
fail at startup or block dispatch on reload. No tracker, workspace, command,
prompt, or agent process behavior changes.

## Weakest point

Dual-read code remains until the legacy form is deliberately retired, so every
new Claude setting must be added to one shared typed parser rather than one path
at a time. The semantic comparison prevents drift today, but the migration needs
a later removal criterion; otherwise compatibility code becomes permanent and
the shipped template never proves the provider envelope in production.
