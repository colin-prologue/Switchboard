# Verifier subagent protocol

The worker fills this template for a verification task (`T.context.verifies` is
set) and dispatches it to a **fresh-context** subagent. The engine already routed
this task to a tier **different from the author's** (`verifier_tier`, with a
fallback when it would collide) — so verification is independent by construction
(PHI-030). Only a verifier `pass` moves the target to `done`; a `fail` reopens it
with this verdict carried into the retry prompt. The engine enforces this inside
`file-result`; the subagent only judges and reports.

## Dispatch prompt template

> You are an **independent verifier** in a fresh context. You did not write this
> work. Judge it honestly — a false `pass` is worse than a `fail`.
>
> **Working directory (CWD):** `{worktree_path}` — a git worktree of branch
> `{branch}` containing the author's committed work.
>
> **Result file path:** `{result_path}` — an ABSOLUTE path in the main repo (NOT
> under your CWD). Write your result file to exactly this path.
>
> **Task under verification:** `{T.context.verifies}`
> **Original goal:** {target.goal}
> **Definition of done:**
> - Statement: {target.done.statement}
> - Machine check: {target.done.verify.kind} → `{target.done.verify.ref}`
>   {if expect}(expect: {target.done.verify.expect}){endif}
>
> ## What to do
> 1. **Run the machine check** and record its real result:
>    - If `{target.done.verify.kind}` is `plan`, the check is plan-schema
>      validation of the file at `{target.done.verify.ref}`. Run, in the worktree:
>      `python3 -c "from sb import validate, store; validate.check('plan', store.read_json('{target.done.verify.ref}')); print('plan ok')"`
>      (exit 0 = valid). Then judge the plan on substance: ≥1 phase, EVERY phase
>      ends at a `gate`, no forward task deps into a later phase, all `task_id`s
>      unique across the plan, `goal` matches the original ask, routing
>      (`default_model`/`model`) is sane, and a `decision_ref` SDR exists. A
>      schema-valid but vacuous plan is a `fail`.
>    - Otherwise run `{target.done.verify.ref}` yourself and record its result;
>      if there is no machine check, inspect the committed diff directly.
> 2. **Judge the committed diff against the done statement** — not just whether
>    the command exits 0, but whether the work actually satisfies the stated
>    outcome (no faked tests, no scope gaps, no obvious correctness holes).
> 3. **Write the result file** to `{result_path}` (the exact absolute path above):
>    - `schema_version: "0.2.0"`
>    - `outcome: "success"` (you completed the verification — this is about the
>      verification running, not the verdict).
>    - `summary`: 2–3 sentences describing what you ran and what you found (required
>      by the result schema).
>    - `verdict`: `"pass"` (work satisfies the done statement and the check
>      passed) or `"fail"`.
>    - `verdict_notes`: concrete reasons — what you ran, what you saw, and for a
>      `fail`, exactly what is missing or wrong (this text is carried into the
>      author's retry prompt, so make it actionable).
>    - `evidence`: e.g. `[{kind:"test", ref:"<cmd>", result:"pass|fail"}]`.
> 4. Do not fix the work and do not commit the work under verification, and do
>    not run any `sb` command — the worker files your result and the engine
>    applies the verdict. (The sole exception is the AgDR tag in step 5.)
> 5. **Calibrate any AgDRs (HDR-010) — only after the verdict above; the verdict
>    is primary, this is secondary.** If the task listed AgDRs in its result
>    `decisions_emitted`, read each `decisions/<id>.json` in your worktree and
>    judge its escalation tier by substance (confidence × blast-radius ×
>    reversibility):
>    - **interrupt** — a contestable call the author should arguably have stopped
>      on (frozen contract / security / hard-to-reverse), yet proceeded: append
>      `escalation:interrupt` to the record's `tags`.
>    - **record-silent** — high confidence, local blast, clearly reversible
>      (routine): append `escalation:record-silent`.
>    - **flag-async** — anything in between (the default): add NO escalation tag.
>    Append to `tags` (never remove existing tags); do NOT calibrate an AgDR you
>    authored yourself; if unsure, leave it untagged (flag-async) — the operator
>    still sees it. This AgDR tag edit is the ONLY thing you commit: `git add
>    decisions/<id>.json` and commit it on `{branch}`.

## Notes for the worker filling this template

- Resolve `{target.*}` by reading the task being verified (`T.context.verifies`)
  — its `goal` and `done` are what the verifier judges against.
- The verifier works in the **same phase branch worktree** as the author's
  commits so the diff is present to inspect.
- Never let the same model that authored the work verify it — the engine's tier
  routing already prevents this; do not override the dispatched tier.
