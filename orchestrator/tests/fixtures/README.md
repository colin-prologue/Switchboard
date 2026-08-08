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
   recovers or terminally fails (WebSocket → HTTPS fallback). See the follow-up
   ticket on early-break behavior in `codex_runner`.

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
