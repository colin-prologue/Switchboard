# AgDR-030: Codex `error` events are non-terminal notifications

**Status:** proposed (2026-08-08, issue #114)
**Surfaces:** `codex_runner` event loop, `TurnResult.failure_class` consumers
(ProviderCircuit, retry/backoff, session accounting), `AgentEvent` stream

## Context

Issue #109 captured real codex-cli `0.146.0-alpha.3.1` output verbatim
(`orchestrator/tests/fixtures/codex_cli_auth_401.jsonl`). It shows the CLI
emitting eleven `{"type":"error","message":"Reconnecting... N/5 (…)"}` events
mid-turn, falling back WebSocket → HTTPS, and only then emitting its own
terminal `turn.failed`. The CLI treats those events as blips and keeps going.

`codex_runner` did not: it broke out of the read loop and classified on the
FIRST `error` event (`codex_runner.py:375-390` @ `ce5764f`). A recoverable
reconnect became a terminal failed turn — killing a healthy session
mid-recovery, burning a retry/session, and feeding ProviderCircuit transient
noise instead of the CLI's own final verdict. Under a provider wobble that is
exactly backwards: the circuit opens on the noise rather than on the outage.

AgDR-025 fixed the *taxonomy* of normalized error strings and AgDR-026 made
`failure_class` the circuit's only input; neither settled *which* stream event
supplies the verdict. That is the ambiguity this decision closes.

## Decision

`error` events are non-terminal. The runner logs one (`AgentEvent` of kind
`notification`, payload `{"type": "error", "text": <truncated detail>}`) and
remembers the terminal-most error's `code`/`detail`. Terminal state comes only
from `turn.failed`, `turn.completed`, or stream end.

**EOF contract.** EOF with at least one remembered `error` event yields
`status="failed"`, `error="codex_error"` (string unchanged — it is the same
condition, reached later in the stream), and `failure_class` classified from
the remembered terminal-most error. `error="port_exit"` / `RUNNER_PROTOCOL`
stays reserved for EOF with no error event seen, preserving its meaning as
"the port died without saying anything".

Provider diagnostic text still never enters `result.error`; it appears only in
the event payload and the raw transcript, exactly as before.

**Demotion is consumer-safe.** The only `AgentEvent` consumer is
`scheduler.py:1386` `_on_agent_event`, which branches solely on
`session_started` and sets `entry.last_event_at` on *every* event. Demoting
`error` from `turn_failed` to `notification` therefore preserves the liveness
heartbeat and cannot regress the stall watchdog (`stall_timeout_ms`).

## Rejected options

### Amendment 2026-08-26 — this record predicted PR #166's round-7 defect

The third rejection below says treating EOF-after-error as `port_exit` would
*"collapse a real provider auth outage into `RUNNER_PROTOCOL`, which AgDR-026
excludes from circuit triggers — reopening the original bug from the other end."*

The **Claude** runner did precisely that until 2026-08-26: a stream carrying a
provider error that closed without a terminal record fell to
`RUNNER_PROTOCOL`/`port_exit`, spent the issue's budget, and left the circuit
closed. Codex review found it on PR #166; this record had named it, and the
mechanism, three weeks earlier.

The rejection was correct, provider-general, and confined to the Codex record.
Nothing propagated it. The SWEEP-2026-08-26-rejection-rationale.md sweep's headline finding.

- **Keep breaking on `error`, but only for non-`Reconnecting` text.** Steelman:
  it is a one-line change and preserves fast failure on "real" errors. Rejected
  because it reintroduces string-sniffing at the transport layer — the exact
  defect AgDR-025 removed from the scheduler — and the ground truth shows the
  *terminal* text (`unexpected status 401 …`) is a substring of the *transient*
  text. Any such filter is a guess about a CLI-internal retry policy that the
  CLI already announces via `turn.failed`.
- **Count reconnect attempts and fail after N.** Steelman: bounds the wait when
  a CLI wedges mid-reconnect. Rejected because `turn_timeout_ms` and
  `stall_timeout_ms` already bound it, and the heartbeat now keeps working —
  a second, weaker timeout keyed on an undocumented counter buys nothing.
- **Treat EOF-after-error as `port_exit`.** Steelman: EOF is EOF; one code path
  is simpler. Rejected because it discards the classification the CLI handed us
  and would collapse a real provider auth outage into `RUNNER_PROTOCOL`, which
  AgDR-026 excludes from circuit triggers — reopening the original bug from the
  other end.

## Blast radius

Codex only; Claude runner untouched, classification patterns and
`FailureClass` membership untouched (#109 owns those), retry policy untouched.
Turns that previously failed on a transient blip now either succeed or fail
with the CLI's own verdict — strictly fewer failed turns, never more. The
Stage 7 canary (`scripts/codex-circuit-canary.sh`) injects a terminal
`turn.failed` as of #109, so its determinism is unaffected.

## Weakest point

The EOF-after-error path assumes a bare `error` event followed by EOF means the
same thing the pre-change code assumed it meant immediately. If some future CLI
version emits a *terminal-meaning* bare `error` and then keeps the stream open
doing nothing, the turn now waits for `turn_timeout_ms` instead of failing
fast — trading a wrong-fast answer for a slow-correct one. That is the right
trade for a circuit input, but it is a real latency change, and the only honest
way to re-check it is a refreshed capture, not reasoning about the CLI.
