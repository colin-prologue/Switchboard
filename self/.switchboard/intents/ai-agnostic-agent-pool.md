# Product intent: AI-agnostic agent pool

- **Slug:** `ai-agnostic-agent-pool`
- **Status:** active; Stage 6 isolated mixed-pool validation is complete. Five
  named live checkpoints prove explicit Claude and Codex, deterministic
  automatic routing to each provider, and rollback to the default Claude-only
  path. Every synthetic handoff merged and closed its issue. Stage 7 Slice 1's
  observability implementation is under review; Claude-only production remains
  unchanged.
- **Decision:** Codex starts with ChatGPT subscription authentication. API-key
  billing is deferred until production throughput or reliability requires it
  (AgDR-016).

## Resume here

- **Current stage:** Stage 7 Slice 1 implementation. The accepted
  [AgDR-025](../../.decisions/AgDR-025-provider-observability-taxonomy.md)
  contract merged in [PR #95](https://github.com/colin-prologue/Switchboard/pull/95)
  at `a0b125a`. The implementation adds typed outcome/failure fields,
  conservative provider-owned classification, and stable provider lifecycle
  logs without changing retry, parking, fallback, routing, or project bindings.
- **Production mode:** Claude-only by default. Existing commands, workflows,
  and project bindings do not pass `--provider codex` or `--provider mixed`
  and remain unchanged.
- **What is enabled:** a process may explicitly select `--provider codex` with
  a strict, Codex-only `providers.codex` workflow. Startup, hot reload,
  timeout/stall/budget policy, credentials, continuation, retry, cancellation,
  capacity, parking, lifecycle logs, and raw JSONL transcript capture all use
  the selected runner. An explicit mixed process now supports durable-label,
  operator-label, and SHA-256 weighted selection, writing a new assignment
  before `status:in-progress`; it also honors configured provider caps and
  retains that assignment through scheduler recovery paths.
- **What remains deliberately disabled:** fallback, registration-script
  support, any mixed-process launch against an existing production repository,
  and any automatic Codex routing weight above zero outside the dedicated inert
  evidence workflow. The completed checkpoint issues must not be rerun.
- **Last verified source:** Stage 7 Slice 1 implementation commit `8406f03`,
  based on accepted contract commit `a0b125a`. The branch passes
  `UV_CACHE_DIR=/private/tmp/switchboard-uv-cache
  uv run python -m pytest -q` from `orchestrator/`: 400 tests in 12.25s on
  2026-07-22. Its focused classifier/Claude/Codex/contract/selector/scheduler
  suite passes 139 tests in 6.33s. `git diff --check` is clean.
- **Stage 7 Slice 1 evidence:** every failed adapter result carries a closed
  `FailureClass`; success carries none. Claude and Codex classify explicit
  authentication, plan, credit, rate-limit, and availability signals while
  near-matches and unknowns remain `worker_failure`. Scheduler lifecycle logs
  carry stable provider/outcome/failure fields for dispatch, completion,
  failure, cancellation, assignment refusal, capacity refusal, and stalls.
  Tests prove classified failures retain the same retry attempt and session
  accounting, and existing parking, sticky assignment, capacity, no-fallback,
  and Claude-only paths remain unchanged. Raw provider diagnostics do not enter
  normalized scheduler errors.
- **Stage 6 Slice 3 verification:** explicit provider caps block only that
  provider, preserve a new durable assignment while capacity is full, and do
  not launch a worker or fall back. A durable assignment selects the same
  provider from continuation/failure/stall retry, hot reload, and a fresh
  orchestrator fixture even when weights favor the other provider. The full
  suite confirms the legacy Claude-only and Codex-only paths remain unchanged.
- **Stage 6 Slice 4 live verification:** all four native-terminal checkpoints
  reached `status:human-review`, opened a scoped fixture PR, passed their full
  external unittest suite, and stopped before merge. Explicit Claude
  [issue #1](https://github.com/colin-prologue/switchboard-mixed-canary/issues/1)
  persisted `provider:claude` and merged [PR #2](https://github.com/colin-prologue/switchboard-mixed-canary/pull/2)
  as `8506ac1` with 3 tests. Explicit Codex
  [issue #3](https://github.com/colin-prologue/switchboard-mixed-canary/issues/3)
  persisted `provider:codex` and merged [PR #4](https://github.com/colin-prologue/switchboard-mixed-canary/pull/4)
  as `f927cd6` with 5 tests. Unlabeled zero-weight Codex
  [issue #5](https://github.com/colin-prologue/switchboard-mixed-canary/issues/5)
  selected `provider:claude` and merged [PR #6](https://github.com/colin-prologue/switchboard-mixed-canary/pull/6)
  as `cc74e56` with 7 tests. Rollback
  [issue #7](https://github.com/colin-prologue/switchboard-mixed-canary/issues/7)
  dispatched `provider_id=claude` through the default CLI path while preserving
  its existing `provider:codex` audit label, then merged [PR #8](https://github.com/colin-prologue/switchboard-mixed-canary/pull/8)
  as `18c8e19` with 9 tests. Each merge automatically closed its issue and each
  handoff branch was deleted.
- **Slice 4 operational observation:** one rollback polling snapshot briefly
  exposed both `status:todo` and `status:in-progress`; the next snapshot
  contained only `status:in-progress`, and the checkpoint completed normally.
  Preserve this as evidence that separate GitHub label mutations are observable
  between writes. It did not duplicate dispatch or alter provider ownership.
- **Stage 6 checkpoint 5 live verification:** unlabeled
  [mixed-canary issue #9](https://github.com/colin-prologue/switchboard-mixed-canary/issues/9)
  used the dedicated `claude: 0, codex: 100` workflow. Weighted selection wrote
  durable `provider:codex` before `status:in-progress`, dispatched
  `provider_id=codex`, retained at least one raw JSONL transcript, passed all 11
  fixture tests, and stopped at `status:human-review`. Its scoped
  [PR #10](https://github.com/colin-prologue/switchboard-mixed-canary/pull/10)
  merged as `14fe89a`, automatically closed issue #9, and its branch was
  deleted. No `agent:*` override appeared, the normal `100/0` workflow was not
  edited, and no existing project used mixed mode.
- **Last end-to-end evidence:** [canary issue #1](https://github.com/colin-prologue/switchboard-codex-canary/issues/1)
  dispatched as `provider_id=codex`, session `019f6325-7419-75e0-b33d-13dbba7407c0`,
  reached `status:human-review`, and opened clean
  [canary PR #2](https://github.com/colin-prologue/switchboard-codex-canary/pull/2).
  The App bot committed `a6130c5`; its branch changes only `greeting.py` and
  `tests/test_greeting.py`. The PR branch passes `python3 -m unittest discover
  -s tests -v` (3 tests). A 23-line raw JSONL transcript exists under the
  workspace-local, git-excluded `.run/transcripts/`; the foreground process was
  then stopped, with no further work dispatched.
- **Continuation evidence and blocker:** canary PR #2 merged as `c726fd0`.
  [Canary issue #3](https://github.com/colin-prologue/switchboard-codex-canary/issues/3)
  dispatched session `019f632c-9737-7e72-9e34-5e2e755b8524`. Its first turn
  created only the git-excluded `.run/continuation-ready` marker and stopped;
  the scheduler resumed it in a second Codex invocation (three raw transcripts,
  36 JSONL lines total, including a final safe retry after the issue stayed
  active). The resumed turn removed the marker, changed only `greeting.py` and
  `tests/test_greeting.py`, and passed `python3 -m unittest discover -s tests
  -v` (5 tests). It could not run `git add` because the managed workspace
  profile rejected `.git/index.lock` with `Operation not permitted`; the bot
  posted the blocker without committing, pushing, opening a PR, or weakening
  the sandbox. The foreground process was stopped and an operator moved the
  issue to non-dispatchable `status:blocked`; preserve this workspace and do not
  manually commit its retained diff.
- **Native restart recovery:** native-terminal launcher reused issue #3's
  preserved dirty workspace without resetting it. The first native worker
  session (`019f634c-2056-7fe0-bd88-0d1833ee7447`) exhausted its turn loop
  while the issue remained active; a second session
  (`019f634e-db6d-75e3-af6c-80a092809a4b`) committed `47bf7f4`, pushed
  `switchboard/issue-3`, opened clean
  [canary PR #4](https://github.com/colin-prologue/switchboard-codex-canary/pull/4),
  and moved the issue to `status:human-review`. PR #4 was subsequently merged
  as `c30ff5e`. The final workspace is clean,
  the continuation marker is absent, and its PR branch passes the five-test
  standard-library suite. The launcher stopped the foreground process at handoff.
  Twenty-one retained JSONL transcripts (247 records) preserve both the failed
  desktop attempt and native recovery. Some native turns posted duplicate
  blocker comments before the later successful handoff; treat that as an
  operational observation to bound in the failure/parking scenario, not as a
  reason to weaken the sandbox.
- **Failure/retry/parking evidence:** [canary issue #7](https://github.com/colin-prologue/switchboard-codex-canary/issues/7)
  started from an absent workspace using a temporary native-terminal workflow
  whose `providers.codex.command` named an intentionally nonexistent executable.
  `after_create` cloned the fixture and `before_run` prepared the issue branch;
  all three sessions then failed as `codex_not_found` at 16:34:01, 16:34:13,
  and 16:34:35 UTC on 2026-07-16. The scheduler retried after 10 seconds and
  20 seconds, then parked the fourth dispatch decision after the 40-second cap
  retry. The issue has durable `status:parked`, no `status:in-progress`, and
  one bot parking comment; its preserved workspace is clean and no pull request
  exists. The launcher stopped the foreground process after validation.
- **Rejected launcher attempts:** issues #5 and #6 are intentionally preserved
  at `status:parked` but are not runner-level evidence. #5 omitted exported
  `SB_HOME`, so the hook path resolved to `/hooks/before_run.sh`; #6 created
  `.run` before launch, making the workspace look pre-existing and skipping
  `after_create`. Both still demonstrated bounded retries and parking without
  source changes, and led to the #7 launcher guard that refuses a pre-existing
  workspace.
- **Credential-refresh evidence:** [canary issue #9](https://github.com/colin-prologue/switchboard-codex-canary/issues/9)
  completed two raw native Codex `exec`/`resume` transcripts in one session
  (`019f6be3-c96a-78f0-a7f1-206b832a159e`). Each turn used the injected GitHub
  App installation token to read the installed repository and recorded only
  `colin-prologue/switchboard-codex-canary` in git-excluded
  `.run/credential-refresh-turn-{1,2}`. The ready marker was removed, the
  fixture workspace remained clean, no PR exists, and the App bot posted one
  completion comment before moving the issue to `status:human-review`. The
  native launcher then stopped the foreground process. This combines with the
  automated forced-401 and turn-length-TTL tests to cover both deterministic
  re-mint logic and live installation-token use without exposing a token.
- **Rejected credential probe:** issue #8 remains `status:blocked`. It used
  `gh api user`, which returned HTTP 403 because that authenticated-user endpoint
  does not accept the App installation token; the token itself could still read
  and comment on the installed canary repository. The launcher timed out because
  it waited only for `status:human-review` while the agent safely retained the
  active issue after the expected blocker. Treat this as a launcher stop-condition
  lesson, not a credential refresh failure.
- **Native-terminal capability probe:** from the macOS Terminal app, the bundled
  ChatGPT Codex CLI ran with subscription login, `--ask-for-approval never`,
  `--ignore-user-config`, and `--sandbox workspace-write` in disposable
  `/private/tmp/switchboard-codex-git-probe.jFrtlq`. It committed `PROBE.md` as
  `9d3bdba` (`codex git probe`) with no bypass or push. The desktop-managed child
  profile remains unsuitable because it explicitly mounts `.git` read-only;
  AgDR-022 adopts native-terminal launch for the isolated canary.
- **Local git capability evidence:** a disposable Codex run under the merged
  `workspace-write` profile created and committed `handoff.txt` successfully in
  `/tmp/switchboard-stage5-git-probe.HtYewt` (commit `0385556`, session
  `019f5e0e-1c7c-7001-9ad8-ee21c0382c05`). This is host evidence, not a
  portability guarantee; `.git` may be protected in other environments.
- **Live canary infrastructure:** user-created
  `colin-prologue/switchboard-codex-canary` is private. The host's ChatGPT
  Codex login is healthy, `gh` was re-authenticated, and a read-only mint
  verified `switchboard-agent[bot]` can access the repository.
- **External canary fixture:** seeded on `main` at `8bb83ca` with a
  standard-library `greeting.py`, one passing unittest, and no dependencies.
  [Issue #1](https://github.com/colin-prologue/switchboard-codex-canary/issues/1)
  and PRs #2 and #4 are merged. Standard gate-state labels are installed.
- **Next single task:** review and merge the Stage 7 Slice 1 implementation PR.
  Confirm the closed taxonomy, conservative false-positive boundary, stable
  lifecycle fields, and unchanged retry/session behavior. Slice 2 circuit
  behavior remains a separate decision and is required before an
  existing-project pilot.
- **Do not dispatch:** a mixed process against an existing project or another
  mixed-canary issue until Stage 7's observability gate says how operators detect
  provider-specific quota/failure and when to invoke Claude-only rollback. Do
  not rerun checkpoints 1 through 5.

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
- Focused binding/verifier tests passed (3 in 0.72s); the full suite passed
  (317 in 12.91s) on 2026-07-17. The external repository is seeded; issue #1
  completed a real Codex handoff to merged PR #2. Issue #3 proves a real
  continuation and native-terminal restart recovery, handing off to merged PR
  #4 with its workspace clean and transcripts preserved as evidence. Issue #7
  proves three `codex_not_found` failures, exponential retry, and durable
  session-cap parking with one comment and no PR. Issue #9 proves two native
  Codex continuation turns can use the real App installation token to access
  the installed repository; its workspace is clean, no PR exists, and it ends
  in `status:human-review`. Stage 5B is complete.

### Stage 6 - Mixed pool

**Purpose:** add deterministic weighted selection, provider concurrency limits,
and explicit issue overrides after both adapters are independently trusted.

**Status:** complete. Slice 3 merged as [PR #89](https://github.com/colin-prologue/Switchboard/pull/89).
Slice 4's inert binding and procedure merged as [PR #90](https://github.com/colin-prologue/Switchboard/pull/90)
and [PR #91](https://github.com/colin-prologue/Switchboard/pull/91); all four
isolated live checkpoints passed and their handoffs merged. Their evidence
merged as [PR #92](https://github.com/colin-prologue/Switchboard/pull/92). Keep
the current Claude-only launch path as the default. Checkpoint 5 subsequently
proved automatic Codex routing through the accepted AgDR-024 evidence workflow.

**Accepted policy:** [AgDR-023](../../.decisions/AgDR-023-stage6-mixed-routing-policy.md)
defines durable `provider:*` assignments, `agent:*` overrides, deterministic
weights, provider caps, no cross-provider fallback, and an isolated mixed
canary rollout.

**Slice 1 evidence (2026-07-17, merged as [PR #87](https://github.com/colin-prologue/Switchboard/pull/87)):**

- The CLI accepts an explicit `--provider mixed`; the omitted flag remains
  Claude-only and `--provider codex` remains the Codex-only canary mode.
- Mixed startup validates exactly `providers.claude`, `providers.codex`,
  `routing.weights`, and optional provider caps that cannot exceed the global
  cap. Invalid or incomplete envelopes fail before polling.
- Mixed mode was validation-only while the routing contract was absent. Its
  P1 review fix proved startup reconciliation could not mutate a board in that
  interim mode.
- Focused tests passed (89) and the full suite passed (327).

**Slice 2 evidence (2026-07-17, merged as [PR #88](https://github.com/colin-prologue/Switchboard/pull/88)):**

- A single durable `provider:claude` or `provider:codex` label wins over any
  later `agent:*` label. An unassigned issue uses one `agent:*` label, then a
  stable SHA-256 bucket of its immutable node ID and `routing.weights`.
- Conflicting or unsupported `provider:*`/`agent:*` labels refuse before a
  claim. A newly selected assignment is written before `status:in-progress`;
  a failed assignment write leaves the issue unclaimed and has no workspace or
  worker side effect.
- The scheduler reserves an issue in memory while awaiting a new assignment
  write, preventing a concurrent poll or retry from starting duplicate work.
- A fresh selector instance reuses an existing durable assignment even if the
  current default weights favor the other provider. Per-provider capacity and
  full retry/reload stickiness are intentionally Slice 3 work.
- Focused tests passed (97) and the full suite passed (335).

**Slice 3 evidence (2026-07-20, merged as [PR #89](https://github.com/colin-prologue/Switchboard/pull/89)):**

- A configured provider cap is a limit on live `RunningEntry` instances for
  that provider; omitted caps inherit the global worker cap. The global cap
  remains the outer scheduler limit.
- If the selected provider is full, a new `provider:*` label remains durable
  but the issue is left unclaimed, no worker starts, and the scheduler never
  falls back to the other provider. The temporary in-memory assignment
  reservation is released on this capacity refusal.
- The common retry timer used by normal continuation, worker failure, and stall
  recovery fetches the issue anew and reselects its durable provider label.
  Focused tests also prove hot reload and a fresh orchestrator instance keep
  `provider:codex` despite `claude: 100, codex: 0` weights.
- Focused tests passed (102) and the full suite passed (340).

**Slice 4 binding (review branch `codex/stage6-mixed-canary-binding`):**

- Adds only a checked-in `mixed-canary` binding and composed template for the
  future private `colin-prologue/switchboard-mixed-canary` repository. The
  binding is constrained to one global worker and one per-provider slot.
- Its initial weights are exactly `claude: 100, codex: 0`. Regression coverage
  validates that policy, both provider envelopes, the durable-label prompt
  guard, and template composition; setup verification allowlists the template.
- A mixed-canary-only provisioning command idempotently creates all gate-state,
  operator `agent:*`, and durable `provider:*` labels. Its tested dry-run is
  offline, and the preflight makes label provisioning mandatory before launch.
- This PR does not create the external repository, grant App access, start a
  process, create an issue, or dispatch a worker. Those are the next reviewed
  operational slice after this binding merges.
- The binding/setup suite passed (7 in 1.12s) and the full orchestrator suite
  passed (344 in 11.34s) on 2026-07-20.

**Slice 4 provisioned baseline (2026-07-20):**

- `colin-prologue/switchboard-mixed-canary` is private. The
  `switchboard-agent` installation can write contents, issues, and pull
  requests. All 13 required status, gate, operator, and provider labels exist.
- Its `main` branch is seeded at `5f48d2c` with a dependency-free greeting
  fixture and one passing unittest. The local seed clone is clean. No issues,
  pull requests, workspaces, or agent launches exist.
- The reviewed procedure runs explicit Claude, explicit Codex, unlabeled
  `claude:100/codex:0`, and default Claude-only rollback as four separate native
  terminal checkpoints. Each invocation enforces phase order and one open item,
  stops the process at named outcomes, and preserves logs/workspaces for review.
- This procedure does not raise the Codex routing weight. A nonzero weight is a
  later reviewed rollout only after the checkpoint evidence passes.
- The focused checkpoint/binding/CLI suite passed (15 in 1.01s), the setup
  verifier reported no failures, and the full orchestrator suite passed (352 in
  13.03s). No live checkpoint was launched during procedure verification.

**Slice 4 live evidence (2026-07-21, source `3f4694a`):**

- The explicit-Claude, explicit-Codex, zero-weight-Codex, and Claude-only
  rollback checkpoints all reached human review with the expected dispatch and
  durable provider labels. Their fixture suites grew from 3 to 9 passing tests.
- Canary issues #1, #3, #5, and #7 closed automatically after merge. Canary
  PRs #2, #4, #6, and #8 merged as `8506ac1`, `f927cd6`, `cc74e56`, and
  `18c8e19`; all four worker branches were deleted.
- Explicit assignments proved both adapters under one mixed process. The
  unlabeled issue proved deterministic `claude: 100, codex: 0` selection. The
  rollback proved the unchanged default process dispatches Claude without
  rewriting historical `provider:codex` evidence.
- One rollback label poll observed both `status:todo` and
  `status:in-progress` between separate tracker mutations. It settled on the
  next poll, caused no duplicate worker, and is retained as an operational
  observability note for Stage 7.
- No existing project used mixed mode, no nonzero Codex weight was enabled, and
  the production Claude-only launch remained unchanged.

**Slice 5 deterministic nonzero-weight evidence (2026-07-22):**

- Accepted [AgDR-024](../../.decisions/AgDR-024-deterministic-nonzero-codex-canary.md)
  adds a separate inert `WORKFLOW.weighted-codex.md` with `claude: 0,
  codex: 100`. One unlabeled issue therefore proves automatic Codex assignment
  without relying on chance or creating routing-probe issues.
- Checkpoint 5 retains the existing native one-at-a-time preflight and stop
  conditions, requires checkpoints 1 through 4 closed, verifies durable
  `provider:codex`, rejects any `agent:*` override, and requires a raw Codex
  transcript plus a clean handoff PR.
- The `0/100` workflow is evidence-only and selected by name for this phase.
  The checked-in mixed-canary baseline remains `100/0`, so stopping the process
  restores the baseline without editing a workflow. No existing project is in
  scope.
- Live issue #9 satisfied that contract, and PR #10 merged as `14fe89a` after
  all 11 fixture tests passed. Stage 6 is complete.

**Test:** weighted selection, capacity, `agent:claude`/`agent:codex` overrides,
sticky retries, reload, unavailable-provider handling, and immediate rollback to
Claude-only mode. Begin with Codex opt-in or low weight.

### Stage 7 - Operational hardening

**Purpose:** make mixed execution observable and production-ready.

**Status:** Slice 1 implemented and awaiting review. Accepted
[AgDR-025](../../.decisions/AgDR-025-provider-observability-taxonomy.md) splits
the work into an observability-only taxonomy followed by separately reviewed
circuit behavior. Slice 1 adds provider-tagged lifecycle outcomes and explicit
subscription failure classes without changing selection, retry, parking,
fallback, or any project binding. Its full 400-test suite and focused 139-test
suite pass. Slice 2 must use that contract to prevent provider availability
failures from burning issue retries before a pilot.

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
