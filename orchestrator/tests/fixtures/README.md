# Ground-truth CLI output fixtures

Real captured provider-CLI output. Never hand-author or edit these files —
hand-authored fixtures are the defect class issue #109 exists to remove. To
refresh, re-run the capture command and commit verbatim.

## codex_cli_auth_401.jsonl

- **CLI:** codex-cli `0.146.0-alpha.3.1` (`/Applications/ChatGPT.app/Contents/Resources/codex`)
- **Captured:** 2026-08-08, operator host
- **Command:** `CODEX_HOME=<empty dir> codex exec --json --skip-git-repo-check "say hi"`
  (empty `CODEX_HOME` isolates the probe from the real login — the CLI sees no
  auth and fails with 401 without touching operator credentials)
- **Exit code:** 1

What it proves (issue #109 findings):

1. Real `turn.failed` events carry `error.message` **only** — no `error.code`
   and no top-level `code`. `_CODEX_CODES` is unreachable on this CLI version.
2. Real intermediate `error` events carry a **top-level `message`** (not a
   nested `error` object): `{"type":"error","message":"Reconnecting... 2/5 …"}`.
3. The real auth-failure text is `"unexpected status 401 Unauthorized: Missing
   bearer or basic authentication in header, …"` — matched by the
   `401 unauthorized` / `missing bearer or basic authentication` patterns
   (added by #109; prior auth patterns did not match, classifying a real auth
   outage as WORKER_FAILURE and keeping the provider circuit closed).
4. Transient `Reconnecting... N/5` error events occur mid-turn before the CLI
   recovers or terminally fails (WebSocket → HTTPS fallback). Fixed in issue
   #114 / AgDR-030: `codex_runner` now treats `error` events as non-terminal
   notifications and takes the verdict from `turn.failed` / `turn.completed` /
   stream end. This file is replayed verbatim by the `replay_fixture`
   scenario in `tests/fake_codex.py` to hold that behavior.

## Conditions verified vs unverified (codex)

| Condition | Status |
|---|---|
| authentication (401, logged out) | **verified** — this fixture |
| context exhaustion | **verified** — real string captured in #47 (`test_failure_classification.py`) |
| usage/plan limit | UNVERIFIED — not safely reproducible on demand; regexes are best-effort |
| credits exhausted | UNVERIFIED — same |
| rate limit | UNVERIFIED — same |
| service unavailable | UNVERIFIED — same |

Per #109's assumptions: unverified conditions are recorded here rather than
covered by hand-written "real-looking" fixtures.

## claude_cli_auth_logged_out.jsonl

- **CLI:** claude-code `2.1.226`
- **Captured:** 2026-08-09, operator host
- **Command:** `CLAUDE_CONFIG_DIR=<empty dir> claude -p --verbose --output-format stream-json "say hi"`
  (isolated config — the CLI sees no auth; the operator's real login untouched)
- **Exit code:** 1

What it proves (issues #116, #165): the logged-out CLI emits an event with
`"error": "authentication_failed"` (`is_api_error_message: true`, and
`message.model: "<synthetic>"`) — a code present in neither `_CLAUDE_CODES`
entry as of #116 (`authentication_expired` / `authentication_required`), and
added by #165 — on the ASSISTANT record, while its TERMINAL
record carries `subtype: "success"` with `is_error: true`,
`terminal_reason: "api_error"`, and `result: "Not logged in · Please run
/login"`. Pre-#116 the runner branched on `subtype` alone, so the run returned
`succeeded` and classification was never reached; the runner now takes the
outcome from the record's own `is_error` and classifies the gated record over
`result` (falling back to `terminal_reason`), where the existing
`\bnot logged in\b` pattern matches. Captured as a
deliberate zero-token isolated probe: contains no conversation content or
secrets (the after_run transcript-privacy policy governs session transcripts,
not probe fixtures — #109 precedent).

| Condition (claude) | Status |
|---|---|
| authentication (logged out) | **verified** — this fixture |
| authentication (OAuth session EXPIRED) | OBSERVED — see below, no probe capture |
| transport drop mid-response (`server_error`) | OBSERVED — see below, no probe capture |
| usage/plan limit, credits, rate limit | UNVERIFIED — not safely reproducible on demand |

The `is_error` outcome gate (issue #116) catches the remaining unverified
conditions regardless of their per-condition strings — they fail the turn as
`WORKER_FAILURE` (retry with backoff) until real text is captured and a
matching pattern is added.

## OBSERVED-but-not-captured claude conditions (issue #165)

Two conditions were seen in **production session transcripts**, not in an
isolated probe. They get no fixture file: this directory holds probe captures
that can be replayed byte-for-byte, and hand-authoring a "real-looking" one is
the defect class #109 exists to remove. Recorded here instead, per the same
precedent that governs the UNVERIFIED rows above.

Source: `switchboard-workspaces/civ-life/8/.run/transcripts/`, five consecutive
verify sessions on civ-life #8 (2026-08-16 → 08-19), every one of which failed
on infrastructure and was charged to the ticket's session budget until it
parked at 5/5.

| Seen on the synthetic assistant record | `error` code | Text |
|---|---|---|
| 2026-08-17, -18, -19 | `authentication_failed` | `Failed to authenticate: OAuth session expired and could not be refreshed` |
| 2026-08-16 (×2) | `server_error` | `API Error: Connection closed mid-response. The response above may be incomplete.` |

Both carry `message.model: "<synthetic>"`, the same marker the logged-out
fixture shows — which is what lets #165 read the typed code without letting
model-authored content reach the circuit.

**What is NOT established:** whether either run's TERMINAL record sets
`is_error`. The session transcript format does not contain the `result` record,
and neither condition is reproducible on demand. #165 therefore handles BOTH
shapes — the gated one and the `is_error`-absent one — rather than assuming the
shape the logged-out probe happens to show. If a real capture of either is ever
taken, it belongs here as a proper fixture and the corresponding branch in
`runner.py` can be narrowed.
