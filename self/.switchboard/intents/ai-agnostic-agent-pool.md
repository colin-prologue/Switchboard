# Product intent: AI-agnostic agent pool

- **Slug:** `ai-agnostic-agent-pool`
- **Status:** active; Stage 1 implementation in progress on issue #65.
- **Decision:** Codex starts with ChatGPT subscription authentication. API-key
  billing is deferred until production throughput or reliability requires it
  (AgDR-016).

## Resume here

- **Current stage:** Stage 1 - neutral runner contract, implementation and
  independent review on `codex/stage1-agent-runner-contract`.
- **Production mode:** Claude-only. No Codex runner is dispatchable.
- **What is enabled:** the existing `claude:` workflow binding and
  `ClaudeRunner` path only.
- **What remains deliberately disabled:** provider-neutral configuration,
  Codex execution, pool selection, provider fallback, and mixed dispatch.
- **Last verified source commit:** merged `main` at `aab0719`; Stage 1 working
  tree based on that commit.
- **Last passing command:** `uv run --project orchestrator python -m pytest
  orchestrator/tests -q` - 258 passed in 9.03s on 2026-07-12 after independent
  review fixes.
- **Last end-to-end evidence:** issue #62 -> PR #63 ->
  `status:human-review`; CI `test` passed. The worker used Claude session
  `7c58c430-8e39-4684-93f6-1436cf65408e` and needed no workspace repair.
- **Next single task:** commit, push, and open the issue #65 PR with focused,
  full-suite, and independent-review evidence.
- **Do not advance until:** the Stage 1 PR passes CI and human review. Keep the
  orchestrator stopped; provider configuration remains a Stage 2 concern.

Update this section at the end of every migration session. A future session
must be able to continue from it without reconstructing prior chat context.

## What + why

Switchboard currently turns GitHub issues into isolated Claude worker sessions.
The scheduler, tracker, workspace lifecycle, retry, parking, and gate-state
methodology are mostly provider-neutral, but execution is bound directly to
Claude CLI configuration, stream-json events, resume flags, permission hooks,
cost accounting, transcript storage, tests, and operator documentation.

The goal is to support a pool containing both Claude and Codex workers while
keeping the existing Claude-only service deployable throughout the migration.
The migration follows a strangler sequence: first place the current Claude
behavior behind a neutral contract without changing it, then implement Codex
beside it, canary Codex in a separate project process, and only then add mixed
selection.

## Binding constraints

- **No flag day.** Every stage must leave the Claude-only workflow runnable.
- **Legacy config remains valid.** Existing `claude:` project bindings continue
  to work until a separately approved breaking release removes them.
- **Provider assignment is sticky.** Once a claim starts, continuations and
  retries use the same provider. A mid-run failure never silently hands a
  partially modified workspace to another provider.
- **No mutating shadow run.** Claude and Codex never work the same issue or
  workspace concurrently. Canary work uses synthetic tickets or a separate
  project binding.
- **Provider-specific safety, neutral invariants.** Each adapter may implement
  its own sandbox and approval mechanisms, while the orchestrator always fixes
  cwd to the issue workspace, injects credentials only into the subprocess
  environment, bounds execution time, and kills the process group on exit.
- **Subscription first.** The Codex canary uses persisted ChatGPT login and
  consumes the plan's Codex allowance/credits. Credentials are never copied
  into a workspace. API-key login is a later operational mode, not a prerequisite
  for the migration.
- **Evidence before progression.** A stage is complete only when its focused
  tests, the full suite, and its stated manual evidence pass at a named commit.

## Stage ledger

### Stage 0 - Claude baseline

**Purpose:** prove the behavior being preserved before changing boundaries.

**Test:**

```bash
uv run --project orchestrator pytest orchestrator/tests/test_runner.py -q
uv run --project orchestrator pytest orchestrator/tests/test_integration.py -q
uv run --project orchestrator pytest orchestrator/tests -q
```

The explicit `orchestrator/tests` path is required when invoked from the repo
root: it makes pytest discover `orchestrator/pyproject.toml`, including
`asyncio_mode = "auto"`. A bare `pytest -q` from the repo root does not load
that configuration and reports the undecorated async tests as unsupported.

Confirm prompt delivery, resume, timeout/process-group cleanup, GitHub token
injection, cost/usage normalization, retry, parking, and one real Claude ticket
reaching PR handoff. Record the issue and PR before completing the stage.

**Automated evidence (2026-07-12, source HEAD `bcab2c9`):**

- `test_runner.py`: 16 passed in 1.37s.
- `test_integration.py`: 33 passed in 4.95s.
- Full `orchestrator/tests`: 256 passed in 9.30s.
- A bare `pytest -q` from the repository root produced 82 async-test failures
  because it did not discover the nested pytest configuration. This was a test
  invocation error, not a source failure; the canonical command above includes
  the test path and passed.
- Manual issue-to-PR evidence: passed; details below.

**Manual evidence (2026-07-12):**

- Issue [#62](https://github.com/colin-prologue/Switchboard/issues/62) was filed
  at `status:todo` with `gate:triage-passed`.
- The unmodified Claude-only runtime at `bcab2c9` created the workspace, started
  session `7c58c430-8e39-4684-93f6-1436cf65408e`, committed `5741a82`, pushed
  `switchboard/issue-62`, and opened
  [PR #63](https://github.com/colin-prologue/Switchboard/pull/63).
- The issue reached `status:human-review` with `status:in-progress` removed.
  PR #63 changes only `README.md`, and its `test` CI check passed.
- First launch without isolation also discovered eligible backlog issues #15,
  #35, #57, and #61. It was stopped cleanly; stale `status:in-progress` labels
  on #15 and #61 were restored to `status:todo`. The successful retry used a
  temporary `baseline:stage0` required label and otherwise-identical workflow,
  proving only #62 dispatched. The temporary issue/repository label was removed
  afterward. Future canaries must use an isolated project/repo or an explicit
  required-label binding; a separate worktree alone does not isolate tracker
  claims.

### Stage 1 - Neutral runner contract

**Purpose:** extract an `AgentRunner` contract while preserving the Claude
adapter's commands and behavior.

**Test:** Claude passes the shared runner contract; generated commands remain
unchanged; scheduler construction remains Claude-only; full suite passes.

**Working evidence (2026-07-12, base `aab0719`, issue #65):**

- The new contract test first failed at collection because
  `orchestrator.agent_runner` did not exist.
- After the minimal protocol extraction,
  `test_agent_runner_contract.py` passed (2 tests: shared behavior and explicit
  Claude-only scheduler construction).
- Contract + existing Claude runner + scheduler integration suites passed
  together; the final focused gate also includes dispatch-guard fakes (55 tests
  in 4.39s).
- Full `orchestrator/tests` passed after review fixes (258 tests in 9.03s).
- Independent Terra 5.6 High review identified overly strict neutral event
  ordering, a misleading runtime-checkable Protocol assertion, and fake runners
  missing provider identity. These were resolved by allowing optional expected
  session IDs, requiring only the neutral terminal event ordering, relying on
  static Protocol typing plus explicit behavioral assertions, and adding
  `provider_id = "fake"` to scheduler substitutes. The exact six-parameter call
  shape remains deliberate: adapter-specific options belong in constructor
  configuration. Stage 1 is not complete until the PR passes its human gate.

**Ticket draft:**

- **Intent:** Introduce a provider-neutral runner boundary around the existing
  Claude implementation so later providers can be added without changing the
  scheduler's execution contract.
- **Acceptance:** define an `AgentRunner` protocol matching the currently used
  `run_turn` surface; make `ClaudeRunner` satisfy it; add a reusable runner
  contract test exercised by Claude; prove generated commands, emitted events,
  continuation, credential injection, timeout, and cancellation are unchanged;
  keep workflow parsing and scheduler selection Claude-only; pass the focused
  runner/integration tests and all `orchestrator/tests`.
- **Non-goals:** provider configuration, Codex runner code, pool selection,
  renaming the legacy `claude:` block, transcript changes, or fallback behavior.

### Stage 2 - Dual-read provider configuration

**Purpose:** accept a new `providers:` schema while translating legacy
`claude:` configuration into the same internal model.

**Test:** old/new config equivalence, conflict rejection, unchanged project
startup, last-known-good hot reload, and full suite.

### Stage 3 - Injectable scheduler

**Purpose:** make provider selection explicit while selection still always
returns Claude.

**Test:** fresh dispatch, continuation, retry, cancellation, parking, capacity,
and logs all retain the Claude provider identity; one real Claude ticket and the
full suite pass.

### Stage 4 - Standalone Codex adapter

**Purpose:** implement `codex exec --json` and resume normalization without
registering Codex for dispatch.

**Test:** fake Codex success, malformed output, timeout, cancellation, resume,
missing binary, credential environment, and process-group cleanup; shared
runner contract; disposable local-repository smoke test; full suite. Production
registry still cannot select Codex.

### Stage 5 - Codex canary project

**Purpose:** exercise the complete workflow in a separate Codex-only project
process using ChatGPT subscription authentication.

**Test:** synthetic triage routes, implementation-to-PR handoff, continuation,
timeout/retry, session cap/parking, credential refresh behavior, transcript
capture, and restart with an in-progress workspace. Several tickets must finish
without manual repair.

### Stage 6 - Mixed pool

**Purpose:** add deterministic weighted selection, provider concurrency limits,
and explicit issue overrides after both adapters are independently trusted.

**Test:** weighted selection, capacity, `agent:claude`/`agent:codex` overrides,
sticky retries, reload, unavailable-provider handling, and immediate rollback to
Claude-only mode. Begin with Codex opt-in or low weight.

### Stage 7 - Operational hardening

**Purpose:** make mixed execution observable and production-ready.

**Test:** provider metrics, usage-limit classification, circuit breaking,
credential expiry, restart recovery, transcript handling, and rollback drills.
Only a pre-start failure may fall back automatically; a failure after workspace
mutation remains pinned or parks.

## Session closeout checklist

Before ending any migration session:

1. Update **Resume here** with the exact stage and next single task.
2. Record the tested commit and commands, including failures.
3. Leave future-stage features disabled by default.
4. Note any manual canary issue/PR evidence.
5. Do not mark a stage complete when only focused tests have run.

## Non-goals

- Replacing the GitHub tracker or gate-state methodology.
- Running two providers concurrently on one issue.
- Automatic provider selection based on subjective task quality in the first
  mixed-pool release.
- API-key billing, centralized spend allocation, or production SLA guarantees
  before the subscription-backed canary produces evidence that they are needed.
