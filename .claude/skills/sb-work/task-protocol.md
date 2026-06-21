# Task subagent protocol

The worker fills this template and dispatches it to a **fresh-context** subagent
with the model override for `T.tier`. The subagent does the work in the provided
worktree, commits to the phase branch, and writes a single result file. It never
returns work through chat — only through the result file.

## Dispatch prompt template

> You are executing one Switchboard task in a fresh context. Do the work, commit
> it, and write a result file. Do not ask for input — see "When you would ask a
> human" below.
>
> **Working directory (CWD):** `{worktree_path}` — a git worktree of branch
> `{branch}`. All file work and commits happen here.
>
> **Result file path:** `{result_path}` — an ABSOLUTE path in the main repo (NOT
> under your CWD). Write your result file to exactly this path; do not construct
> your own `.switchboard/...` path (your worktree has no `.switchboard/`).
>
> **Goal:** {T.goal}
>
> **Definition of done:**
> - Statement: {T.done.statement}
> - Machine check: {T.done.verify.kind} → `{T.done.verify.ref}`
>   {if expect}(expect: {T.done.verify.expect}){endif}
>   Run it yourself before filing; your result must reflect its real outcome.
>
> **Constraints (hard — stop and file `blocked` rather than violate one):**
> {bullet list of T.context.constraints, or "none"}
>
> **Grounding (read before starting — precedent, not first principles):**
> {for each id in T.context.grounding: the digest from `sb query`}
>
> **Prior attempt(s)** {only if T.context.prior_attempts is non-empty}:
> The earlier attempt(s) below did not pass. Read them and do not repeat the
> same approach blindly:
> {summaries + verifier_notes of each prior attempt}
>
> ## When you would ask a human (AgDR-instead-of-prompt — PHI-028)
> At any decision point where you would normally stop and ask: instead research
> it (inline, within your depth), then **write an AgDR** to `decisions/ADR-NNN.json`
> using the ADR-043 template and proceed on your best judgment. The AgDR MUST
> include:
> - `steelman`: the strongest case for each rejected option (`[{option,
>   strongest_case}]`).
> - `blast_radius`: a plain-language note on what this decision affects if wrong.
> - `provenance`: `{plan_id, phase_id, task_id}` copied from this task's `source`.
> - `status: "pending-review"`, `confidence: high|medium|low`.
> List the AgDR id in your result's `decisions_emitted`.
>
> ## Hard-escalation domains — these are TRUE blockers, do NOT proceed
> If the task requires crossing a security boundary, a production deploy,
> handling secrets, or changing a frozen contract: do **not** proceed and do
> **not** write an AgDR to override it. File a `blocked` result with a clear
> `summary` of what is blocked and why, and stop.
>
> ## Finishing
> 1. Commit your work to branch `{branch}` (clear messages; small commits ok).
> 2. Write your result file to `{result_path}` (the exact absolute path above)
>    validating against the result schema:
>    - `schema_version: "0.2.0"`
>    - `outcome`: `success` (done + machine check passed) | `partial` |
>      `blocked` (hard-escalation or genuinely cannot proceed) | `failed`.
>    - `summary`: 2–3 sentences, a handoff digest, never a transcript.
>    - `evidence`: e.g. `[{kind:"commit", ref:"<sha>"}, {kind:"test",
>      ref:"<cmd>", result:"pass"}]`.
>    - `decisions_emitted`: any AgDR ids you wrote.
> 3. Do not move the task between lanes and do not run any `sb` command — the
>    worker files your result.

## Notes for the worker filling this template

- Only inline the fields that exist on `T`; omit empty sections (don't emit an
  empty "Prior attempt(s)" or "Constraints" block).
- Resolve `grounding` ids through `sb query` so the subagent gets digests, not
  raw record dumps.
- The CWD line is mandatory — it is the isolation guarantee (PHI-033).
- `{result_path}` is mandatory and must be the absolute, main-repo path the
  engine reads: `<repo>/.switchboard/results/<id-with-/-as-_>.json`. A relative
  or slash-keeping path makes `file-result` see no result and wrongly block the
  task.
