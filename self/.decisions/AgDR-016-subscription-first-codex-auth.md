# AgDR-016: Start Codex canary with ChatGPT subscription authentication

**Status:** accepted (2026-07-12)
**Surfaces:** future Codex runner configuration, service setup, usage telemetry,
and `self/.switchboard/intents/ai-agnostic-agent-pool.md`

## Context

The AI-agnostic agent-pool migration needs an authentication and billing mode
for headless Codex workers. The installed Codex CLI supports both persisted
ChatGPT login and API-key login. ChatGPT-authenticated CLI work consumes the
account's Codex allowance/credits; API-key authentication is separately metered
through the API account.

The first objective is to validate adapter correctness and workflow fit, not to
establish production throughput or cost allocation. Requiring API billing before
the canary would add account, secret, budget, and operational work before there
is evidence that the mixed pool is worth operating in production.

## Decision

Build and canary the Codex adapter using persisted ChatGPT subscription
authentication. The orchestrator launches the non-interactive Codex CLI under
the service user's existing login; it does not copy authentication material into
issue workspaces or add an API key to project configuration.

Treat plan usage exhaustion, credit exhaustion, and authentication expiry as
distinct provider-availability failures in logs and telemetry. They must not be
misclassified as ticket implementation failures or trigger cross-provider
handoff after a workspace has been modified.

Keep API-key authentication as a future provider credential mode. Reconsider it
when canary evidence shows a need for predictable unattended throughput,
separate cost attribution, centralized credentials, or production support
expectations.

## Rejected options (steelmanned)

- **Require API-key billing from the first Codex test.** This gives explicit
  metering, service-oriented credentials, and clearer cost ownership. Rejected
  for the canary because it expands setup and secret-management scope before
  validating the adapter or workload. It remains the likely production path if
  subscription limits are operationally constraining.
- **Support both authentication modes in the first adapter release.** This
  appears flexible and avoids a later config change. Rejected because it doubles
  the initial credential/error test matrix and makes it harder to distinguish
  protocol defects from billing/auth defects during the canary.
- **Treat subscription limits as ordinary retries.** This minimizes scheduler
  changes. Rejected because repeated retries consume session caps without making
  progress and hide an operator-level capacity problem as an issue-level failure.

## Blast radius

No current runtime behavior changes. The decision applies only after a Codex
adapter and canary project exist. Claude remains the sole production provider
through Stages 0-4, and mixed scheduling remains separately gated.

## Weakest point

Persisted user login and subscription credits are less predictable for an
always-on service than API credentials and separately budgeted API usage.
Concurrency limits, shared agentic usage, login refresh, or credit exhaustion
may interrupt the canary. That interruption is accepted as useful evidence for
the production-auth decision, provided Switchboard reports it accurately and
does not burn issue retries or switch providers mid-claim.
