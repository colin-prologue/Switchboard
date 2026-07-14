# Product intent: AI-agnostic agent pool

- **Slug:** `ai-agnostic-agent-pool`
- **Status:** active; Stage 5B readiness is implemented on issue #77. PR #78's
  Codex-template CI repair is locally verified and awaits its replacement CI run
  and human review before any live Codex ticket is dispatched.
- **Decision:** Codex starts with ChatGPT subscription authentication. API-key
  billing is deferred until production throughput or reliability requires it
  (AgDR-016).

## Resume here

- **Current stage:** Stage 5B readiness - the private canary repository and App
  access exist; transcript capture and the inert Codex-only binding, including
  template-aware compose-drift verification, are in review.
- **Production mode:** Claude-only by default. Existing commands, workflows,
  and project bindings do not pass `--provider codex` and remain unchanged.
- **What is enabled:** a process may explicitly select `--provider codex` with
  a strict, Codex-only `providers.codex` workflow. Startup, hot reload,
  timeout/stall/budget policy, credentials, continuation, retry, cancellation,
  capacity, parking, lifecycle logs, and raw JSONL transcript capture all use
  the selected runner.
- **What remains deliberately disabled:** mixed provider maps, weighted or
  per-issue selection, fallback, registration-script support, and any Codex
  process against an existing production repository.
- **Last verified source commit:** Stage 5B readiness code `afa6c97`, based on
  merged Stage 5A `main` at `7926a14`; an unpushed PR #78 repair adds the
  declared `codex-canary` workflow-template guard.
- **Last passing command:** `orchestrator/.venv/bin/python -m pytest -q` from
  `orchestrator/` - 317 passed in 10.73s on 2026-07-14. Focused canary binding
  and verifier tests: 3 passed in 0.25s; `bash scripts/verify-setup.sh` reported
  zero failures.
- **Last end-to-end evidence:** issue #71 -> PR #72 ->
  `status:human-review`; CI `test` passed. The selector dispatched
  `provider_id=claude`, session `0efa3a2c-db48-45d0-83d8-a4f7f1be77b8`
  committed `e6d7d98`, and no workspace repair was needed.
- **Local git capability evidence:** a disposable Codex run under the merged
  `workspace-write` profile created and committed `handoff.txt` successfully in
  `/tmp/switchboard-stage5-git-probe.HtYewt` (commit `0385556`, session
  `019f5e0e-1c7c-7001-9ad8-ee21c0382c05`). This is host evidence, not a
  portability guarantee; `.git` may be protected in other environments.
- **Live canary infrastructure:** user-created
  `colin-prologue/switchboard-codex-canary` is private and empty on `main`.
  The host's ChatGPT Codex login is healthy, `gh` was re-authenticated, and a
  read-only mint verified `switchboard-agent[bot]` can access the repository.
- **Next single task:** push the PR #78 template-verifier repair, wait for green
  CI and human review, then merge before seeding the external canary fixture.
- **Do not dispatch until:** this readiness PR is green, human-approved, and
  merged. Then seed only synthetic fixture code in the separate repository and
  launch one foreground Codex worker; never point it at Switchboard itself.

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
  workspace concurrently. Canary work uses synthetic tickets in a separate
  repository. A separate binding or required label against the same repository
  isolates dispatch but not repo-wide startup reconciliation.
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
  configuration. Stage 1 passed its human gate and merged as `cc62087`.
- Stage 1 handoff: issue [#65](https://github.com/colin-prologue/Switchboard/issues/65)
  and [PR #66](https://github.com/colin-prologue/Switchboard/pull/66) completed
  the human-review gate; CI passed and the branch was deleted after merge.

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

**Working evidence (2026-07-12, base `cc62087`, issue #67):**

- The new workflow/reload tests first failed in eight expected places: provider
  blocks were ignored, conflicts did not raise, invalid envelopes were accepted,
  and a conflicting hot reload continued dispatch.
- `providers.claude` now resolves through the same path-aware typed parser as
  legacy `claude:`; semantically equal dual forms pass and unequal forms fail.
- Focused workflow + integration + reload suites passed after review fixes
  (107 tests in 3.52s).
- Full `orchestrator/tests` passed after review fixes (277 tests in 9.43s).
- `workflow/WORKFLOW.base.md` and the composed project workflow remain on the
  legacy form; scheduler construction remains Claude-only.
- Independent Terra 5.6 High review found malformed provider fields that could
  remove the cost cap, duplicate YAML keys that could bypass conflict detection,
  and stale resume wording. Strict provider validation, duplicate-key rejection,
  and this ledger correction resolve those findings. Re-review then caught
  duplicate detection running after YAML merge expansion; detection now runs on
  textual keys before SafeLoader preserves `<<` inheritance and explicit
  overrides. PR review then identified that merge-only source mappings still
  escaped inspection; commit `ebeb575` recursively checks merge sources before
  flattening and adds a focused regression. The PR human gate remains.
- Stage 2 handoff: issue [#67](https://github.com/colin-prologue/Switchboard/issues/67)
  and [PR #68](https://github.com/colin-prologue/Switchboard/pull/68) passed the
  human gate and merged as Stage 2 base `548fa4a`. Its first CI run exposed a Stage 1
  success-contract flake: the fake subprocess had a 1-second cold-start
  deadline. Follow-up `4b0ffbe` uses production-like startup bounds while
  leaving dedicated timeout tests unchanged; the subsequent CI run passed.

### Stage 3 - Injectable scheduler

**Purpose:** make provider selection explicit while selection still always
returns Claude.

**Test:** fresh dispatch, continuation, retry, cancellation, parking, capacity,
and logs all retain the Claude provider identity; one real Claude ticket and the
full suite pass.

**Working evidence (2026-07-13, base `548fa4a`, issue #69):**

- New selector tests first failed at collection because no
  `orchestrator.runner_selector` module existed.
- `AgentRunnerSelector.select(Config, Issue)` is now injected through the
  `Orchestrator` constructor. `ClaudeOnlyRunnerSelector` is the sole production
  implementation and returns `ClaudeRunner(cfg.claude())` for both config forms.
- Selection occurs before claim or tracker mutation and once per worker session;
  a failure regression proves the issue remains unclaimed and unlabeled.
- Integration harnesses inject their fake runner through the selector. Dispatch,
  multi-turn continuation, retry, cancellation, parking, capacity, credentials,
  shutdown, guard, and reload paths pass unchanged. Provider identity is retained
  on `RunningEntry` and present in lifecycle logs.
- Focused selector + runner-contract + scheduler suites passed (55 tests in
  3.64s). Full `orchestrator/tests` passed (282 tests in 10.21s).
- AgDR-018 records dispatch-time selection and its boundary. Codex config,
  registration, pooling, fallback, and sticky cross-retry assignment remain
  disabled. Direct `cfg.claude()` policy reads are explicitly deferred debt.
- [PR #70](https://github.com/colin-prologue/Switchboard/pull/70) passed CI and
  the human gate, then merged as Stage 3 base `6d18b04`.
- Manual canary [issue #71](https://github.com/colin-prologue/Switchboard/issues/71)
  was the only issue carrying temporary `canary:stage3`; a one-worker workflow
  required that label. The Stage 3 process logged `provider_id=claude`, started
  session `0efa3a2c-db48-45d0-83d8-a4f7f1be77b8`, created a clean workspace,
  committed `e6d7d98`, pushed `switchboard/issue-71`, and opened
  [PR #72](https://github.com/colin-prologue/Switchboard/pull/72). The issue
  reached `status:human-review`, PR CI passed, and the transcript was captured
  under the workspace's `.run/transcripts/` directory. No repair was needed.
- The required label isolated worker dispatch but not the startup sweep: startup
  reconciliation reverted issue #69 from `status:in-progress` to `status:todo`
  before #71 dispatched. No unrelated worker launched, #69 was restored
  immediately, and the temporary label was deleted after clean shutdown. Future
  provider canaries must use a separate repository because startup reconciliation
  is intentionally repo-wide under the one-process-per-repo invariant.

### Stage 4 - Standalone Codex adapter

**Purpose:** implement `codex exec --json` and resume normalization without
registering Codex for dispatch.

**Test:** fake Codex success, malformed output, timeout, cancellation, resume,
missing binary, credential environment, and process-group cleanup; shared
runner contract; disposable local-repository smoke test; full suite. Production
registry still cannot select Codex.

**Working evidence (2026-07-13, base `6d18b04`, issue #73):**

- Installed `codex-cli 0.144.0-alpha.4` reports `Logged in using ChatGPT`.
  The current official manual and local help agree on `exec --json`,
  `thread.started`, `item.*`, terminal turn events, and `exec resume`.
- New adapter tests first failed at collection because `orchestrator.codex_runner`
  did not exist. `CodexRunner` now launches directly with cwd fixed to the
  workspace, prompt on stdin, a process group, bounded startup/turn deadlines,
  and JSONL normalization into the shared `AgentRunner` contract.
- `CodexConfig` defaults to approval policy `never`, `workspace-write`, and
  workspace-write network access. Runs ignore user config but inherit saved
  `CODEX_HOME` auth; `CODEX_API_KEY` and `OPENAI_API_KEY` are removed while a
  per-turn GitHub token overlays only `GITHUB_TOKEN` and `GH_TOKEN`.
- Fake-process coverage includes fresh success, resume argv/stdin, failed/error
  events, malformed and non-object JSON, missing session, protocol EOF, missing
  binary, startup/turn timeout, cancellation/process-group cleanup, stderr, and
  credential environment. Codex passes the reusable runner success contract.
- Focused adapter + contract + selector + workflow suites passed (85 tests in
  1.27s). Full `orchestrator/tests` passed (297 tests in 9.56s).
- Real subscription smoke passed in disposable git repository
  `/tmp/switchboard-stage4-codex-smoke.gvEuVw`: fresh and resumed turns both
  succeeded under session `019f5e05-d112-72e3-96a1-cea187a3b7f7`, producing
  exactly the two requested lines in one untracked file and no other changes.
- AgDR-019 records the non-registration boundary. Current `workspace-write` may
  protect `.git`; unrestricted local execution is not an acceptable shortcut.
  A Stage 5 host probe later demonstrated git writes under the current profile,
  but the canary still has to verify this in its deployed environment.
- `providers.codex` remains rejected, `ClaudeOnlyRunnerSelector` remains the
  only production selector, and no shipped workflow changed.
- [PR #74](https://github.com/colin-prologue/Switchboard/pull/74) passed CI and
  the human gate, then merged as `42a1f24`. PR #72 merged afterward, making
  `330d5c9` the Stage 5 base. Both branches were deleted.

### Stage 5 - Codex canary project

**Purpose:** exercise the complete workflow in a separate Codex-only project
process using ChatGPT subscription authentication.

**Test:** synthetic triage routes, implementation-to-PR handoff, continuation,
timeout/retry, session cap/parking, credential refresh behavior, transcript
capture, and restart with an in-progress workspace. Several tickets must finish
without manual repair.

#### Stage 5A - opt-in process mode

**Purpose:** make the standalone adapter dispatchable only when an operator
explicitly starts a Codex-only process, while preserving the existing
Claude-only command and workflow behavior.

**Working evidence (2026-07-13, base `330d5c9`, issue #75):**

- New tests first failed at collection because `CodexOnlyRunnerSelector` did
  not exist. `--provider codex` now selects it; omitting the flag still creates
  `ClaudeOnlyRunnerSelector`.
- `providers.codex` is strict and Codex-only (`kind: codex-cli`). Legacy Codex,
  legacy-plus-Codex, mixed provider maps, unsupported kinds, unknown fields,
  and empty commands fail startup. Normal Claude validation is unchanged.
- Timeout, stall, and optional cumulative budget are runner policy. Token TTL
  uses the selected runner's turn timeout, and running entries pin their stall
  limit at dispatch so a hot reload cannot mutate an in-flight session.
- Fake Codex-process integration covers dispatch, multi-turn resume, token
  refresh/TTL, provider logs, failure retry/release, capacity, terminal
  cancellation, and session-cap parking. Existing Claude integration coverage
  remains green.
- `spec/SPEC.md` and AgDR-020 bind the explicit one-provider process mode,
  runner-owned policy, dispatch-pinned stall deadline, separate-repository live
  gate, and non-portability of the local `.git` probe.
- Focused workflow + selector + runner-contract + CLI + integration tests passed
  (119 tests in 3.61s). Full `orchestrator/tests` passed (312 tests in 10.58s).
- No project binding, registration script, GitHub repository, App installation,
  or production process changed. Mixed routing remains Stage 6 work.
- [PR #76](https://github.com/colin-prologue/Switchboard/pull/76) passed CI and
  human review, merged as `7926a14`, and its branch was deleted.

**Return-session test gate:** run the focused 119-test command first to restore
context around the changed boundaries, then the full suite. Confirm a normal
launch without `--provider` still rejects a Codex-only workflow and accepts the
existing Claude workflow. Do not treat fake-runner coverage as live canary
evidence.

```bash
uv run --project orchestrator python -m pytest \
  orchestrator/tests/test_workflow.py \
  orchestrator/tests/test_runner_selector.py \
  orchestrator/tests/test_agent_runner_contract.py \
  orchestrator/tests/test_main.py \
  orchestrator/tests/test_integration.py -q
uv run --project orchestrator python -m pytest orchestrator/tests -q
```

#### Stage 5B - isolated live canary

**Purpose:** provision one separate GitHub repository and project binding, then
run real subscription-authenticated Codex tickets without exposing an existing
board to startup reconciliation or worker mutation.

**Operator gate before any mutation:** confirm repository owner/name,
visibility, and GitHub App installation access. Then test `codex login status`,
one synthetic issue through PR handoff, continuation, failure retry, parking,
credential refresh, transcript capture, restart recovery, and git writes under
the deployed sandbox. Several tickets must finish without manual repair before
Stage 6 planning starts.

**Readiness evidence (2026-07-13, base `7926a14`, issue #77):**

- The user created the private, empty repository
  `colin-prologue/switchboard-codex-canary` with default branch `main`, added it
  to the existing `switchboard-agent` App installation, and repaired host `gh`
  authentication. A read-only installation-token mint then fetched the repo
  successfully; no token was printed or persisted.
- `CodexRunner` now records its raw `codex exec --json` stdout in a timestamped
  workspace `.run/transcripts/codex-*.jsonl` file and adds `.run/` to the
  workspace-local Git exclude. Opening or writing the transcript is best-effort
  and cannot change the worker result. Tests prove terminal JSONL capture,
  exclusion, no injected token in the captured fixture stream, and storage
  failure preservation of a successful turn.
- `projects/codex-canary/` is a checked-in but inert one-worker binding with a
  strict `providers.codex` envelope, normal workspace hooks, and a Codex-specific
  prompt that preserves PR/human-review handoff without Claude allowlist or
  budget language. It is not generated by `register-project.sh`; provider-aware
  registration remains deferred work.
- PR #78 CI correctly rejected the initial binding because its compose-drift
  check assumed every project used `workflow/WORKFLOW.base.md`. The repair makes
  `SB_WORKFLOW_TEMPLATE=codex-canary` explicit, adds the canonical
  `workflow/WORKFLOW.codex-canary.md`, and makes `verify-setup.sh` select only
  allowlisted templates (`base` by default or `codex-canary`), rejecting unknown
  template names and byte-for-byte drift. `register-project.sh` now records the
  explicit `base` default for future normal projects.
- Focused binding/verifier tests passed (3 in 0.25s); the full suite passed
  (317 in 10.73s) on 2026-07-14. The external canary repository has not been
  seeded or dispatched yet.

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
