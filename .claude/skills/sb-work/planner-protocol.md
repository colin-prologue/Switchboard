# Planner subagent protocol

The worker fills this template for a **planner task** (`T.done.verify.kind ==
"plan"`, produced by `sb seed --goal`) and dispatches it to a fresh-context
subagent at `T.tier`. The deliverable is NOT code: it is a plan-schema-valid
`plans/{plan_id}.json` plus a synthesis decision (SDR) recording why the
decomposition/routing was chosen. The worker does not seed the plan — a human
reviews it and runs `sb seed --plan plans/{plan_id}.json`.

## Dispatch prompt template

> You are a **planner** in a fresh context. Decompose one goal into an
> executable, schema-valid plan. You write a plan + a decision record; you do
> NOT write the implementation and you do NOT seed the plan.
>
> **Working directory (CWD):** `{worktree_path}` — a git worktree of branch
> `{branch}`. Write and commit your plan + SDR here.
>
> **Result file path:** `{result_path}` — an ABSOLUTE path in the main repo (NOT
> under your CWD). Write your result file to exactly this path.
>
> **Goal (verbatim — preserve it as `goal` in the plan):** {T.goal}
>
> **Plan id you MUST use:** `{plan_id}`  →  write the plan to `{plan_path}`.
>
> **Grounding (read before planning — precedent, not first principles):**
> {for each id in T.context.grounding: the digest from `sb query`; or "query the
> decision log with `sb query --tags <relevant>` and ground the plan in what you
> find"}
>
> ## What to produce
> 1. A plan at `{plan_path}` that VALIDATES against `schemas/plan.schema.json`
>    (v0.1.0). The schema is strict (`additionalProperties:false` throughout).
>    Required top-level: `schema_version:"0.1.0"`, `plan_id:"{plan_id}"`, `goal`
>    (the verbatim goal above), `created` (ISO-8601), `author:{kind:"model",
>    id:"<your model id>"}`, and `phases` (≥1). Each phase requires `phase_id`
>    (`^PH-[0-9]+$`), `name`, `default_model` (one of fable|opus|sonnet|haiku —
>    the cost lever), and `tasks` (≥1); give every phase a `gate:{type:"human"|
>    "auto", condition:"<expr>"}` (the phase-transition forcing function — every
>    phase ends at a human review gate). Each task requires `task_id`
>    (`^T-[0-9]+$`), `title`, and `done:{statement, verify?:{kind,ref,expect?}}`.
>    Optional but encouraged: task `model` overrides, `depends_on` (intra-plan
>    task DAG; NO forward deps into a later phase — they deadlock behind the
>    gate), `constraints`, `grounding` (ADR/HDR/SDR ids), `open_questions`
>    (blocking → a human must answer before execution; resolve_by:"research" →
>    an agent investigates inside a named phase), and `intent` per phase.
> 2. A synthesis decision at `decisions/SDR-NNN.json` (next free SDR id; scan
>    `decisions/` for the max) validating against
>    `schemas/decision-record.schema.json` (v0.3.0). Required: `schema_version:
>    "0.3.0"`, `id:"SDR-NNN"` (must start `SDR-` because `type` is synthesis),
>    `type:"synthesis"`, `status:"pending-review"`, `timestamp` (ISO-8601),
>    `title`, `author:{kind:"model", id:"<your model id>"}`. Strongly include:
>    `context`, the `options` you weighed for the decomposition/routing, the
>    `chosen` option name, `reasoning`, and `blast_radius`. Set the plan's
>    `decision_ref` to this SDR id.
>
> ## Sizing & routing guidance
> - One task = one scoped, verifiable outcome. If a task needs an "and", split it.
> - Make every `task_id` unique across the WHOLE plan (not just within its
>   phase) — `sb seed` resolves `depends_on` by task id, so a reused id
>   mis-wires the dependency DAG.
> - Route cheap/mechanical tasks to `haiku`/`sonnet`; reserve `opus`/`fable` for
>   design and cross-cutting decisions. `default_model` per phase; override per task.
> - Prefer phases that each end in a reviewable PR (the gate).
>
> ## Before you file — self-validate
> Run this in your CWD and make sure each exits 0:
> ```
> python3 -c "from sb import validate, store; validate.check('plan', store.read_json('{plan_path}')); print('plan ok')"
> python3 -c "from sb import validate, store; validate.check('decision', store.read_json('decisions/SDR-NNN.json')); print('sdr ok')"
> ```
>
> ## Finishing
> 1. Commit `{plan_path}` and `decisions/SDR-NNN.json` to branch `{branch}`.
> 2. Write your result file to `{result_path}` (the exact absolute path above):
>    - `schema_version: "0.2.0"`
>    - `outcome: "success"` (plan + SDR committed and self-validated) | `blocked`
>      (the goal is under-specified to the point you cannot plan it — say what is
>      missing) | `partial`.
>    - `summary`: 2–3 sentences — the shape of the plan (phases, key routing).
>    - `evidence`: `[{kind:"commit", ref:"<sha>"}]`.
>    - `decisions_emitted`: `["SDR-NNN"]`.
> 3. Do not seed, do not move lanes, do not run any other `sb` command — the
>    worker files your result; verification validates the plan independently.

## Notes for the worker filling this template

- `{plan_id}` = `T.source.plan_id`; `{plan_path}` = `T.done.verify.ref`.
- Resolve `grounding` ids through `sb query` so the planner gets digests.
- The CWD line is mandatory (isolation guarantee, PHI-033).
- `{result_path}` must be the absolute main-repo path the engine reads:
  `<repo>/.switchboard/results/<id-with-/-as-_>.json`.
- The planner PROMPT is the lowest-confidence artifact of A-planner (ADR-007):
  it is reviewed-not-tested and gets its live exercise in the D exit bar.
