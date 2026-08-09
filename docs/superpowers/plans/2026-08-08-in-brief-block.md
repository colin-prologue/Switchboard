# "In brief" Plain-Language Block Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a two-field, human-readable `## In brief` block to the top of every agent-written ticket body, PR body, and triage verdict comment, so a reader catching up can tell in twenty seconds what changed and what judgment inside it deserves scrutiny.

**Architecture:** This is a templates-and-prose change, not a code change. The block is authored into the three executable surfaces every agent passes through — the `new-ticket.sh --scaffold` skeleton, the PR-handoff step of the workflow prompt, and the triage NEEDS WORK verdict routing. Enforcement for PR bodies is human, at Gate C, documented in `methodology/METHODOLOGY.md`. Nothing in `orchestrator/src/` changes; the orchestrator's own generated comments are already plain-language and are explicitly out of scope.

**Tech Stack:** Bash (heredoc in `scripts/new-ticket.sh`), Markdown (workflow prompt templates, methodology, decision records), pytest via `uv` for the pinning tests.

**Spec:** [`docs/superpowers/specs/2026-08-08-plain-language-block-design.md`](../specs/2026-08-08-plain-language-block-design.md)

## Global Constraints

- **All anchors verified at HEAD `ce5764f558b2a8a39d078a7b7e144075f70db318`.** Re-verify any `file:line` before editing; if the line moved, find the content, don't trust the number.
- **The only permitted test invocations** are the two pinned prefixes in the worker allowlist (`workflow/WORKFLOW.base.md:61`), run from the workspace root: `uv run --project orchestrator python -m pytest <paths> -q` or `uv run --project orchestrator pytest <paths> -q`. A bare `pytest`, a `python3`, or a `cd X && ...` chain will be denied. Do not retry variants.
- **`workflow/WORKFLOW.base.md` and `projects/switchboard-self/WORKFLOW.md` must be edited in the same commit, with byte-identical body text.** `orchestrator/tests/test_workflow.py:932` (`test_base_and_composed_workflow_are_in_sync`) re-runs `register-project.sh`'s substitution in-process and asserts byte equality. `register-project.sh` is outside the worker allowlist, so recomposition is not available — hand-edit both.
- **Added prompt text must contain no `{{...}}` placeholders.** The composed file substitutes `{{REPO}}` → `colin-prologue/Switchboard`, `{{WORKSPACE_ROOT}}`, `{{MAX_AGENTS}}`, and `{{CONVENTION_ROOT}}` → `self/`. Keeping the new text placeholder-free means the same literal text goes into both files, which is the simplest way to keep the drift test green.
- **The two field labels are exact and load-bearing:** `**What this does:**` and `**What could be wrong:**`. The heading is exactly `## In brief`. These strings are pinned by tests; do not reword, re-case, or re-punctuate them.
- **Existing content is preserved verbatim.** This plan only inserts. No existing section, citation requirement, rubric check, or checklist entry is removed or reworded.
- **Do not touch `orchestrator/src/`.** No Python source changes anywhere in this plan.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `scripts/new-ticket.sh` | Emits the ticket skeleton authors start from; gains the block as its first section | 1 |
| `orchestrator/tests/test_new_ticket.py` | Pins the skeleton's sections and content | 1 |
| `workflow/WORKFLOW.base.md` | The shared agent prompt template; gains the block requirement in the PR-handoff step and the triage verdict routing | 2 |
| `projects/switchboard-self/WORKFLOW.md` | The composed per-project mirror; must match byte-for-byte | 2 |
| `orchestrator/tests/test_workflow.py` | Pins the block text in both prompt files and guards drift | 2 |
| `methodology/METHODOLOGY.md` | Human-readable methodology; gains a "Writing for the reader" section and a Gate C completeness condition | 3 |
| `self/.decisions/AgDR-029-in-brief-plain-language-block.md` | The decision record this change requires | 3 |

**Deviation from the spec, applied here deliberately.** The spec's surfaces table says to add the block "to the drafting-quality checklist as a fifth entry." That is wrong and this plan does not do it. Every entry in that checklist is a *triage reject criterion* — the rubric at `workflow/WORKFLOW.base.md:112-143` bounces tickets on them by name. Adding a writing rule to that list would make triage bounce tickets on prose, which the spec's own non-goals forbid. The block gets its own `## Writing for the reader` section instead, and the checklist is left untouched.

---

## Task 1: Ticket scaffold

**Files:**
- Modify: `scripts/new-ticket.sh:43-68` (the `SKELETON` heredoc)
- Test: `orchestrator/tests/test_new_ticket.py:45`, `:59`, `:143` (modify), plus one new test

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: the canonical block text. Tasks 2 and 3 reproduce the same heading (`## In brief`) and the same two field labels (`**What this does:**`, `**What could be wrong:**`) verbatim. Nothing imports from this task; the coupling is textual.

- [ ] **Step 1: Write the failing test**

Add this new test to `orchestrator/tests/test_new_ticket.py`, immediately after `test_scaffold_pins_drafting_quality_content` (which ends at line 76):

```python
def test_scaffold_leads_with_in_brief_block() -> None:
    # The plain-language layer (spec 2026-08-08) sits ABOVE the citation-dense
    # body, not instead of it. Two rules make the fields hard to pad, and both
    # must reach the author inside the skeleton itself: the identifier ban on
    # the first field, and the if/then consequence shape on the second.
    proc = run("--scaffold")
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    assert out.lstrip().startswith("## In brief"), (
        "## In brief must be the FIRST section of the skeleton, above ## Intent"
    )
    assert "**What this does:**" in out
    assert "**What could be wrong:**" in out
    # Rule 1 — the identifier ban.
    assert "no issue numbers, file paths, AgDR ids" in out
    # Rule 2 — the conditional-and-consequence shape.
    assert '"if X, then Y"' in out
    # The dense body survives underneath, in order.
    assert out.index("## In brief") < out.index("## Intent")
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run --project orchestrator python -m pytest orchestrator/tests/test_new_ticket.py::test_scaffold_leads_with_in_brief_block -q
```

Expected: FAIL — `AssertionError: ## In brief must be the FIRST section of the skeleton, above ## Intent`.

- [ ] **Step 3: Add the block to the scaffold heredoc**

In `scripts/new-ticket.sh`, the heredoc currently opens at line 43 with `cat <<'SKELETON'` and its first content line is `## Intent`. Insert the block between them, so the heredoc begins:

```bash
  cat <<'SKELETON'
## In brief

**What this does:** <one plain sentence, no issue numbers, file paths, AgDR ids,
`status:` label names, or function/field names. If you cannot say it without
them, you do not understand the change well enough to file it yet.>

**What could be wrong:** <one assumption or decision, in "if X, then Y" shape:
name the trigger and what concretely breaks. Naming a quality is not an answer
("coverage could be broader"); naming a consequence is ("if the label API is not
read-your-writes, the read-back false-negatives and the ticket strands").>

## Intent
```

Everything from `## Intent` down to the closing `SKELETON` at line 68 is unchanged.

- [ ] **Step 4: Run the new test to verify it passes**

Run:

```bash
uv run --project orchestrator python -m pytest orchestrator/tests/test_new_ticket.py::test_scaffold_leads_with_in_brief_block -q
```

Expected: PASS.

- [ ] **Step 5: Extend the two existing section-list tests**

The skeleton's section list is pinned in two places and both must learn the new section.

In `test_scaffold_emits_all_sections_and_exits_clean` (line 45), the tuple at lines 49-55 becomes:

```python
    for section in (
        "## In brief",
        "## Intent",
        "## Acceptance criteria",
        "## Non-goals",
        "## Consumers of mutated state",
        "## Assumptions",
    ):
```

In `test_scaffold_output_is_valid_dry_run_body` (line 143), the tuple at line 148 becomes:

```python
    for section in ("## In brief", "## Intent", "## Acceptance criteria", "## Non-goals", "## Assumptions"):
```

- [ ] **Step 6: Run the whole scaffold test file**

Run:

```bash
uv run --project orchestrator python -m pytest orchestrator/tests/test_new_ticket.py -q
```

Expected: PASS, all tests. If `test_scaffold_output_is_valid_dry_run_body` fails, the skeleton is no longer round-tripping as a body — check that the heredoc quoting (`<<'SKELETON'`, single-quoted, no expansion) was preserved and that no backtick or `$` in the new text broke it.

- [ ] **Step 7: Commit**

```bash
git add scripts/new-ticket.sh orchestrator/tests/test_new_ticket.py
git commit -m "feat(scaffold): lead the ticket skeleton with an In brief block"
```

---

## Task 2: Workflow prompt — PR handoff and triage verdict

**Files:**
- Modify: `workflow/WORKFLOW.base.md:154-161` (NEEDS WORK routing) and `:198-210` (step 7, hand off)
- Modify: `projects/switchboard-self/WORKFLOW.md` — the identical edits at the identical locations
- Test: `orchestrator/tests/test_workflow.py` — one new test, after `test_base_and_composed_workflow_are_in_sync` (which ends at line 962)

**Interfaces:**
- Consumes: the block heading and the two field labels defined in Task 1 — `## In brief`, `**What this does:**`, `**What could be wrong:**`. Reproduce them character-for-character.
- Produces: nothing later tasks import. Task 3 references this task's Gate C requirement in prose only.

- [ ] **Step 1: Write the failing test**

Add to `orchestrator/tests/test_workflow.py`, after line 962:

```python
def test_workflow_prompt_pins_in_brief_block():
    """The plain-language block must reach the agent through the prompt itself.

    Pinned in BOTH files: the base template and the composed mirror. The
    sync test above proves they match; this test proves the content is
    actually there, so a well-intentioned "simplification" of either file
    cannot silently drop the requirement while staying in sync.
    """
    repo_root = Path(__file__).resolve().parents[2]
    for rel in ("workflow/WORKFLOW.base.md", "projects/switchboard-self/WORKFLOW.md"):
        text = (repo_root / rel).read_text(encoding="utf-8")
        assert "## In brief" in text, f"{rel}: block heading absent"
        assert "**What this does:**" in text, f"{rel}: first field absent"
        assert "**What could be wrong:**" in text, f"{rel}: second field absent"
        # The PR-handoff step must keep Closes #N ahead of the block: the
        # orchestrator resolves the issue link through GitHub's closing
        # references, so burying it breaks the human-review transition.
        assert "Keep the `Closes #N` line first" in text, f"{rel}: ordering rule absent"
        # Gate C consequence must be stated where the agent reads it.
        assert "is incomplete at the merge gate" in text, f"{rel}: gate consequence absent"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run --project orchestrator python -m pytest orchestrator/tests/test_workflow.py::test_workflow_prompt_pins_in_brief_block -q
```

Expected: FAIL — `AssertionError: workflow/WORKFLOW.base.md: block heading absent`.

- [ ] **Step 3: Edit the PR-handoff step in `workflow/WORKFLOW.base.md`**

Step 7 currently reads (lines 198-201):

```
7. **Hand off, don't self-merge.** Commit, push the branch, open a PR with `gh`
   linking this issue, attach evidence of the criteria passing. Then, as your
   FINAL action, write the handoff evidence file `.run/handoff-evidence.json`
   at the workspace root (issue #61):
```

Replace those four lines with:

```
7. **Hand off, don't self-merge.** Commit, push the branch, open a PR with `gh`
   linking this issue, attach evidence of the criteria passing.

   The PR body opens with an `## In brief` block — two fields, before any other
   section, for a reader who has none of your context:

   > **What this does:** one plain sentence. No issue numbers, file paths, AgDR
   > ids, `status:` label names, or function/field names. If you cannot say it
   > without them, you have not understood your own change well enough to hand
   > it off.
   >
   > **What could be wrong:** one assumption or decision you made, in "if X,
   > then Y" shape — the trigger, and what concretely breaks when it does not
   > hold. Naming a quality is not an answer ("coverage could be broader");
   > naming a consequence is ("if the label API is not read-your-writes, the
   > read-back false-negatives and the ticket strands").

   Keep the `Closes #N` line first, block second: the orchestrator resolves the
   issue link through GitHub's closing references, and burying that line breaks
   your own handoff transition. Everything you would otherwise write goes below
   the block, unchanged — the block adds a layer, it does not replace one.

   A PR body with no block, or whose second field names a quality instead of a
   consequence, is incomplete at the merge gate and will be bounced there, the
   same way a missing AgDR is.

   Then, as your FINAL action, write the handoff evidence file
   `.run/handoff-evidence.json` at the workspace root (issue #61):
```

Lines 202 onward (the JSON fenced block and the "Do NOT edit any `status:*` label yourself" paragraph) are unchanged.

- [ ] **Step 4: Edit the triage verdict routing in `workflow/WORKFLOW.base.md`**

The NEEDS WORK bullet currently reads (lines 154-157):

```
- **NEEDS WORK** → post a feedback comment whose first line is the exact heading
  `## Triage verdict` (grep-able), listing each failed rubric check and the fix,
  then relabel to `status:drafting`. Clear `gate:triage-passed` in the same
  command (every route back to drafting drops the marker — idempotent if absent).
```

Replace those four lines with:

```
- **NEEDS WORK** → post a feedback comment whose first line is the exact heading
  `## Triage verdict` (grep-able). Under it, before the per-check list, write an
  `## In brief` block carrying the same two fields a PR body does:

  > **What this does:** one plain sentence saying what the verdict is and what
  > the author has to change. No issue numbers, file paths, or rubric numbers.
  >
  > **What could be wrong:** the single finding the author has the strongest
  > case to push back on, and why — in "if X, then Y" shape. You are the one
  > adversary here; name where you might be the one who is wrong.

  Then list each failed rubric check and its fix, and relabel to
  `status:drafting`. Clear `gate:triage-passed` in the same command (every route
  back to drafting drops the marker — idempotent if absent).
```

Lines 158-161 (the fenced `gh issue comment` / `gh issue edit` example) are unchanged.

- [ ] **Step 5: Mirror both edits into the composed file**

Apply Steps 3 and 4 to `projects/switchboard-self/WORKFLOW.md` at the same two locations — the NEEDS WORK bullet and step 7. The inserted text is byte-identical: it contains no `{{...}}` placeholders, so nothing needs substituting.

The surrounding lines differ between the two files (the composed file has `colin-prologue/Switchboard` where the base has `{{REPO}}`, and `self/.decisions/` where the base has `{{CONVENTION_ROOT}}.decisions/`). **Do not "fix" those differences** — they are the substitution the sync test performs, and normalizing them will turn the test red.

- [ ] **Step 6: Run the pin test and the drift test together**

Run:

```bash
uv run --project orchestrator python -m pytest orchestrator/tests/test_workflow.py -q
```

Expected: PASS. If `test_base_and_composed_workflow_are_in_sync` fails, the two files diverge — diff them and confirm the only differences are the four placeholder substitutions (`{{REPO}}` → `colin-prologue/Switchboard`, `{{WORKSPACE_ROOT}}` → `/Users/colindwan/Developer/switchboard-workspaces/switchboard-self`, `{{MAX_AGENTS}}` → the composed file's own `max_concurrent_agents` value, `{{CONVENTION_ROOT}}` → `self/`). A trailing-whitespace or blank-line mismatch in the inserted text is the most likely cause.

- [ ] **Step 7: Commit**

```bash
git add workflow/WORKFLOW.base.md projects/switchboard-self/WORKFLOW.md orchestrator/tests/test_workflow.py
git commit -m "feat(workflow): require the In brief block in PR bodies and triage verdicts"
```

---

## Task 3: Methodology and the decision record

**Files:**
- Modify: `methodology/METHODOLOGY.md:65-69` (Gate C)
- Modify: `methodology/METHODOLOGY.md` — new `## Writing for the reader` section, inserted after the "Task-intent / spec in the issue body" section (which ends at line 143) and **before** `## Drafting-quality checklist` (line 145)
- Create: `self/.decisions/AgDR-029-in-brief-plain-language-block.md`
- Test: `orchestrator/tests/test_workflow.py:974` (`test_decision_record_numbers_are_unique_and_match_headings`) — existing, no edit needed

**Interfaces:**
- Consumes: the block heading and field labels from Task 1; the Gate C consequence sentence from Task 2 ("is incomplete at the merge gate"). The methodology prose is the canonical statement the workflow prompt points at.
- Produces: nothing downstream.

- [ ] **Step 1: Verify the decision-record test currently passes and 029 is free**

Run:

```bash
uv run --project orchestrator python -m pytest orchestrator/tests/test_workflow.py::test_decision_record_numbers_are_unique_and_match_headings -q
```

Expected: PASS. Then confirm the next free number is 029:

```bash
ls self/.decisions/
```

Expected: the highest existing is `AgDR-028-orchestrator-owned-terminal-handoff.md`. If a number above 028 exists (a parallel branch merged first), use the next free one and adjust every `029` in this task accordingly — the test asserts the filename number and the `# AgDR-NNN:` heading agree.

- [ ] **Step 2: Add the Gate C completeness condition**

`methodology/METHODOLOGY.md:65-69` currently reads:

```
- **Gate C — final review.** Every implementation hands off at
  `status:human-review`. A human merges. Agents never self-merge. Merge review
  includes ratifying (or overturning) any AgDRs the PR added under
  `<convention_root>.decisions/` — a PR that changed spec/methodology
  semantics without one is incomplete.
```

Append one sentence to that bullet, so it ends:

```
  semantics without one is incomplete. It also checks the `## In brief` block
  (see "Writing for the reader" below): a PR body with no block, or whose
  **What could be wrong** names a quality rather than a consequence, is
  incomplete the same way and bounces the same way.
```

- [ ] **Step 3: Add the "Writing for the reader" section**

Insert this section into `methodology/METHODOLOGY.md` between line 143 (the end of "Task-intent / spec in the issue body") and line 145 (`## Drafting-quality checklist`):

````markdown
## Writing for the reader — the `## In brief` block

Everything else in this methodology optimizes for an implementing agent: exact
citations, enumerated consumers, `file:line` at a named sha. That precision is
load-bearing and stays. It is also unreadable to a human catching up — the
operator between context switches, or somebody helping review the board.

So every agent-written ticket body, PR body, and triage verdict opens with a
fixed, grep-able block carrying **insight, not information**:

```
## In brief

**What this does:** <one sentence>

**What could be wrong:** <one decision or assumption, and what breaks if it is
false>
```

Two rules make the fields hard to pad, and they are the whole mechanism:

1. **"What this does" bans identifiers** — no issue numbers, no file paths, no
   AgDR/ADR/OBS identifiers, no `status:*` label names, no function, class, or
   field names. An author who cannot clear this bar has not understood its own
   change well enough to summarize it. This is what buys the twenty-second
   glance.

2. **"What could be wrong" requires a conditional and a consequence** — the
   *if X, then Y* shape. Naming a quality ("coverage could be broader", "this
   could be more robust") fails; naming a trigger and its damage passes. This is
   the scrutiny surface: it tells a reviewer what to argue with before merging.

**Placement.** The block is the first section, with exactly two exceptions, both
machine-load-bearing: on a PR body, the `Closes #N` line comes first (the
orchestrator resolves the issue link through GitHub's closing references); on a
triage verdict, the `## Triage verdict` heading comes first (it is pinned as the
comment's first line for grep-ability). Everything the author would otherwise
write goes below the block, unchanged. The block adds a layer; it removes
nothing.

**Enforcement is asymmetric on purpose.** PR bodies are gated at Gate C — a
missing or hedged block bounces the PR, the same as a missing AgDR. Ticket
bodies and triage verdicts get the block from their templates but are never
bounced for it: a triage round costs a full dispatched session, and a bounce for
prose does not reduce implementation risk.

The orchestrator's own generated comments (park notices, dispatch refusals) need
no block — they already lead with a plain sentence and a concrete next action,
and are the model this section generalizes.
````

Note the nested fence: the block example above uses a plain triple-backtick fence inside the section. Keep it — `METHODOLOGY.md` is not itself fenced, so this renders correctly.

- [ ] **Step 4: Write the decision record**

Create `self/.decisions/AgDR-029-in-brief-plain-language-block.md`:

```markdown
# AgDR-029: Layer a two-field plain-language block above the citation-dense body

- **Status:** proposed by the plain-language implementation session (2026-08-08);
  awaiting ratification at the PR merge gate.
- **Context:** Agent-written tickets, PRs, and triage verdicts are unreadable at
  a glance. This is produced by rules the repo enforces, not by carelessness:
  the drafting-quality checklist requires `file:line` at a named sha for every
  cited mechanism, and the enumeration of every consumer of mutated state. That
  precision is what stops a dispatched session burning turns on a fictional
  claim, so removing it is not on the table. But it means a human catching up —
  or an outside reviewer — reads mechanism before meaning and cannot tell what a
  change actually is or what deserves argument. The question was at what
  altitude to bind a readability layer, and how to keep it from decaying into
  ceremony.
- **Decision:**
  1. Add a fixed `## In brief` block above the existing body on all three
     agent-written surfaces. Two fields only: **What this does** and **What
     could be wrong**. Nothing existing is removed or reworded.
  2. Make both fields hard to pad by constraint rather than by exhortation. The
     first bans identifiers outright — an author who cannot clear it has not
     understood its own change. The second requires an *if X, then Y* shape, so
     a hedge ("coverage could be broader") is structurally rejectable.
  3. Bind it in the executable surfaces every author passes through — the
     `--scaffold` skeleton and the workflow prompt — not in prose alone, and pin
     the strings with tests. Prose binds only readers; this repo's own #23/#24
     collision is the local proof.
  4. Enforce asymmetrically: gate PR bodies at Gate C, template-only for tickets
     and triage verdicts.
- **Rejected (steelmanned):**
  - *Rewrite the templates so plain language is the primary voice, demoting
    citations to an appendix.* Steelman: the cleanest result, and it removes the
    two-audience compromise entirely. Rejected: it fights the triage rubric and
    the drafting checklist head-on, both of which would need reworking, and it
    risks stripping agents of precision they demonstrably use.
  - *Generate a plain-language digest as a bot comment, leaving the sources
    untouched.* Steelman: zero disruption to existing gates. Rejected: a second
    artifact that drifts from its source, and it does not help the reader
    already staring at the PR body — which is the actual failure moment.
  - *Gate tickets on the block at triage too.* Steelman: consistent, maximum
    teeth, and consistent with the view that multi-round triage revisions are
    the point. Rejected: a triage round costs a full dispatched session, and a
    bounce for prose quality is the one bounce that reduces no implementation
    risk.
  - *Add a third field, "where to look first."* Steelman: it would steer the
    review, not just orient it. Rejected to hold the block at two fields; the
    bet is that "what could be wrong" implicitly names the file worth reading.
    This is the field to add back first if drafts read as thin.
  - *Adding the rule to the drafting-quality checklist as a fifth entry* (as the
    design spec proposed). Rejected during implementation: every entry in that
    list is a triage reject criterion the rubric bounces on by name, so filing a
    writing rule there would make triage bounce tickets on prose — which this
    same decision's enforcement asymmetry exists to prevent. It got its own
    section instead.
- **Blast radius:** `scripts/new-ticket.sh` (skeleton), `workflow/WORKFLOW.base.md`
  + `projects/switchboard-self/WORKFLOW.md` (prompt, both halves of the sync
  pair), `methodology/METHODOLOGY.md` (Gate C + a new section), and three test
  files' worth of pins. No `orchestrator/src/` change; no new state, label, or
  gate. The two canary projects declare their own workflow templates and are
  unaffected.
- **Weakest point:** Gate C enforcement is the reviewer reading it, and the
  scarce resource this protects — reviewer attention — is also its enforcement
  mechanism. A reader skimming is the least likely to catch a padded **What
  could be wrong**. Accepted rather than solved: the identifier ban on the first
  field is self-enforcing and does most of the practical work. If padding turns
  out to be common, the follow-up is a mechanical merge-gate check that greps
  the first field for banned tokens (issue numbers, paths, `AgDR-`, `status:`).
  Building that checker now is speculative — the evidence for whether it is
  needed does not exist yet.
```

- [ ] **Step 5: Run the full suite**

Run:

```bash
uv run --project orchestrator python -m pytest orchestrator/tests -q
```

Expected: PASS, 488+ tests (488 was the count at `ce5764f`; this plan adds two). If `test_decision_record_numbers_are_unique_and_match_headings` fails, the filename number and the `# AgDR-NNN:` heading disagree — they must match exactly.

- [ ] **Step 6: Commit**

```bash
git add methodology/METHODOLOGY.md self/.decisions/AgDR-029-in-brief-plain-language-block.md
git commit -m "docs(methodology): gate the In brief block at Gate C; record AgDR-029"
```

---

## Task 4: Dogfood the block on this change's own PR

**Files:** none — this task produces the PR body.

**Interfaces:**
- Consumes: the block format from Tasks 1–3.
- Produces: the first real instance of the block. If it cannot be written well for this change, the design is wrong and that is worth knowing before it ships.

- [ ] **Step 1: Verify the full suite is green before handing off**

Run:

```bash
uv run --project orchestrator python -m pytest orchestrator/tests -q
```

Expected: PASS. Do not hand off red.

- [ ] **Step 2: Push the branch**

```bash
git push -u origin HEAD
```

- [ ] **Step 3: Open the PR with a body that obeys its own rule**

The body must lead with `Closes #N` (substituting the real issue number if this work is ticketed; omit the line entirely if it is not), then the block, then the usual detail. Use this body:

```markdown
## In brief

**What this does:** Tickets, pull requests, and review verdicts now open with two
plain sentences — what the change is, and what judgment inside it might be wrong.
The detailed, citation-heavy write-up stays exactly as it was, underneath. The aim
is that someone with no context can decide in twenty seconds whether they need to
read further.

**What could be wrong:** The second field is only as good as the reviewer's
attention, and the reviewer's attention is the thing this change exists to
protect. If people skim past a padded "what could be wrong," the field becomes
ceremony and every ticket gets two extra lines of noise for nothing. There is no
automated check — deliberately, since there is no evidence yet about whether
padding actually happens.

## What landed

- **Ticket skeleton** (`scripts/new-ticket.sh`): the block is now the first
  section `--scaffold` emits, above `## Intent`.
- **Agent prompt** (`workflow/WORKFLOW.base.md` + the composed
  `projects/switchboard-self/WORKFLOW.md`): required in PR bodies at the handoff
  step, and in NEEDS WORK triage verdicts. `Closes #N` stays first — the
  orchestrator resolves the issue link through GitHub's closing references.
- **Methodology** (`methodology/METHODOLOGY.md`): a new "Writing for the reader"
  section, and the block added to Gate C's completeness conditions alongside the
  AgDR requirement.
- **Decision record**: AgDR-029.
- **Not changed**: the drafting-quality checklist, the triage rubric, every
  existing citation requirement, and the orchestrator's own generated comments —
  those already lead with a plain sentence and are the model this generalizes.

## Evidence

`uv run --project orchestrator python -m pytest orchestrator/tests -q` → all
passing. New pins: the skeleton leads with the block and carries both rules; both
halves of the workflow sync pair carry the block, the ordering rule, and the gate
consequence.

## Weakest point (from AgDR-029)

Gate C enforcement is a human reading it. Accepted rather than solved — the
identifier ban on the first field is self-enforcing and does most of the work. If
padding shows up in practice, the follow-up is a mechanical grep of the first
field for issue numbers, paths, `AgDR-`, and `status:`.
```

- [ ] **Step 4: Write the handoff evidence file**

As the FINAL action, at the workspace root:

```json
{"issue": "<issue number>", "pr_number": <PR number>, "head_sha": "<git rev-parse HEAD>"}
```

Write it to `.run/handoff-evidence.json`. Do not edit any `status:*` label. Stop.

---

## Self-Review

**Spec coverage.** Every spec section maps to a task: the block definition and its two rules → Task 1 (canonical text) and Task 2 (prompt); the surfaces table → Tasks 1–3 row by row, with orchestrator comments explicitly excluded; enforcement at Gate C → Task 3 Step 2; the drift constraint → Task 2 Steps 5–6 and the Global Constraints; the testing section → Task 1 Steps 5–6 and Task 3 Step 5; the AgDR requirement → Task 3 Step 4. One spec instruction is deliberately not implemented — the "fifth checklist entry" — and the reason is recorded in File Structure and in AgDR-029's rejected options.

**Placeholder scan.** No TBD, TODO, or "similar to Task N". Every code and prose block is the literal text to write. The one intentional variable is the AgDR number, which Task 3 Step 1 tells the implementer how to resolve.

**String consistency.** `## In brief`, `**What this does:**`, and `**What could be wrong:**` appear identically in Tasks 1, 2, 3, and 4 and in the test assertions. The Gate C consequence string `is incomplete at the merge gate` is asserted in Task 2 Step 1 and written in Task 2 Step 3. The ordering-rule string `Keep the \`Closes #N\` line first` is asserted in Task 2 Step 1 and written in Task 2 Step 3.
