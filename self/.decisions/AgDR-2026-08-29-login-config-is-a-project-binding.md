# AgDR-2026-08-29 — Operator and review-bot logins are project bindings, not template literals

- **Status:** proposed (ratify at this PR's merge gate)
- **Issue:** #171
- **Date:** 2026-08-29
- **Supersedes / amends:** amends **AgDR-037** — the "deliberate config edit" that
  takes the review-response loop live is now a tracked `project.env` value rather
  than a hand-edit of a shared template. The *requirement* that going live be a
  deliberate operator act is unchanged; only the artifact that carries it moves.
  Applies **AgDR-048** (single operator) to the shape of the operator field.

## Context

Two shipped, tested features were structurally unreachable for `base`-stance
projects:

- `fold.operator_logins` had **no placeholder in any template**. The scheduler's
  fold sub-poll returns immediately on an empty list, so every `/fold`,
  `/no-fold`, and 👍/👎 signal was inert. The only way to set it was to hand-edit
  a shared template and commit a personal GitHub login into it — or to edit the
  composed file under `.run/`, which is regenerated on every launch.
- `review_response.bot_logins` had a placeholder in `WORKFLOW.prototype.md` and a
  **literal `[]`** in `WORKFLOW.base.md`. `register-project.sh` computed,
  escaped, and substituted `REVIEW_BOT_YAML` into nothing for every `base`
  project, so the loop had never been reachable for `switchboard-self` at any
  configuration — which is why its Codex review threads were answered by hand.

Both are the same defect: a feature whose enabling config has no slot to receive
a value is not "off by default", it is unbuildable. Verified live on 2026-08-25 —
a well-formed `/fold` on #169 sat inert for 25 minutes, and setting
`operator_logins` in the composed workflow made it apply within one minute. The
machinery was never broken; it had no operator.

## Decision

### 1. Both logins are composed from the project binding

`{{OPERATOR_LOGIN_YAML}}` and `{{REVIEW_BOT_YAML}}` appear in **both** shared
stance templates, fed from `SB_OPERATOR_LOGIN` / `SB_REVIEW_BOT` in
`projects/<slug>/project.env` by `register-project.sh` — exactly as
`{{MAX_AGENTS}}`, `{{REPO}}` and `{{CONVENTION_ROOT}}` already work. An unset
variable composes to `[]`, byte-identical to the literal it replaces, so no
existing project's posture changes by adopting the new template.

### 2. Opt-in stays deliberate; the artifact changes

AgDR-037 required going live to be a deliberate config edit, never a merge side
effect. Merging *this* PR is that edit for `switchboard-self` and for nobody
else: the value lives in one project's tracked binding, so enabling a project is
a reviewable one-line diff against a file that already carries its repo and
paths. That is a stronger form of "deliberate", not a weaker one — the previous
mechanism required committing an operator's identity into a template every
project shares.

### 3. `SB_OPERATOR_LOGIN` is singular

`fold.operator_logins` is a list, but the substitution wraps exactly one value in
one pair of quotes. Fed `a,b` it emits `["a,b"]` — a single malformed login the
loader accepts as a well-formed one-element list and that matches nobody.
AgDR-048 makes single-operator the modelled reality rather than a shortcut, so
the variable is named for the shape it has. A project needing several operators
hand-edits its composed `WORKFLOW.md`, and that becomes its own ticket.

### 4. All three recomposers move together

`register-project.sh` (compose), `verify-setup.sh` (drift check) and
`freshness-preflight.sh` (launch-time recompose from origin) each carry their own
hardcoded substitution list. A placeholder added to a template without a feeding
substitution in **all three** is not a partial improvement — it is a red tree:
`verify-setup.sh` fails any project whose composed workflow retains a
`{{[A-Z_]+}}` token, and `freshness-preflight.sh` fails open on an unresolved
placeholder and silently stops adopting origin. The empty-stays-`[]` derivation
is now written **four** times (three scripts plus the test helper). Accepted:
factoring it needs a shared shell library that does not exist, for three lines.

## Rejected alternatives (steelmanned)

- **Hand-edit the composed `projects/<slug>/WORKFLOW.md` and leave the templates
  alone.** Zero mechanism, immediately effective, and it is what the live probe
  on 2026-08-25 did. Rejected: `verify-setup.sh`'s drift check fails a composed
  file that diverges from its template, and `freshness-preflight.sh` overwrites
  it on the next launch precisely because a hand-edited composed file is the bug
  class that ticket exists to kill. The edit would be reverted by design.
- **A comma-splitting multi-valued `SB_OPERATOR_LOGINS`.** Matches the field's
  plural type and needs no follow-up ticket if a second operator appears.
  Rejected: per-element quoting and escaping inside a `sed` one-liner, replicated
  across three scripts, to model a case AgDR-048 says does not exist. The failure
  mode of getting it wrong is silent — a malformed login matches nobody and looks
  exactly like "the operator hasn't reacted yet".
- **Read the logins from the environment at scheduler startup instead of the
  composed workflow.** No template churn, no third recomposer to update.
  Rejected: every other per-project value is composed into the workflow, and a
  second configuration channel means two places to look when detection is inert
  — the exact second-reader failure this project keeps finding.
- **Enable the operator field but leave `bot_logins` for a follow-up.** Smaller
  blast radius: one write-bearing loop goes live instead of two. Rejected: the
  base template's missing `{{REVIEW_BOT_YAML}}` slot is the *same* defect found
  in the same audit, and splitting it would ship a `project.env` whose
  `SB_REVIEW_BOT` still substitutes into nothing — a value that reads as
  configured and does nothing.

## Blast radius

- **Write-bearing loops go live for `switchboard-self` on merge.** Fold-apply
  writes issue bodies, posts marker comments and relabels `drafting → triage`;
  review-response posts PR replies and re-dispatches the implementer (bounded at
  2 rounds by the durable per-PR marker). Both are bounded and both were
  exercised in production on 2026-08-25.
- **Pending-signal exposure.** Enabling detection makes *existing* unactioned
  `/fold` signals actionable at once. The 2026-08-25 pre-enable scan across all
  13 `drafting`/`decision` tickets found exactly one (#169). That is evidence
  from a point in time, not a standing clearance — re-run at merge.
- **No other project changes.** An unset variable composes to `[]`; the only
  binding carrying the new values is `projects/switchboard-self/project.env`.
- **`review_response` also needs `$SB_APP_BOT_LOGIN`.** Absent it the loop still
  disables itself with one log line; this record does not fix that half.
- **Superseded test.** `test_composed_self_workflow_ships_the_response_loop_disabled`
  asserted `bot_logins == ()` for the composed self workflow. It was correct
  under AgDR-037's original mechanism and is replaced by a test asserting the
  exact enabled tuples.

## Weakest point

**The opt-in is now easy enough that it can be granted by inattention.** The old
mechanism was awful, and its awfulness was load-bearing: nobody enables a
write-bearing loop by accident when doing so means committing their GitHub login
into a template every project shares. A one-line addition to a `project.env`
during a routine re-registration reads as boilerplate, and `register-project.sh`
is documented as idempotent and safe to re-run. Nothing in the tooling makes
`--operator-login` feel like the permission grant it is; the only guard is that
the diff is reviewable and the flags default to empty.

Second: three scripts still hold three hand-maintained copies of the same
substitution list. This ticket adds a per-template placeholder-coverage test —
which would have caught the `bot_logins` asymmetry — but there is **no test that
the three substitution lists agree with each other**. `verify-setup.sh`'s drift
check catches it for `verify-setup.sh` only, and `freshness-preflight.sh` fails
*open*, so a future fourth placeholder missed there is silent by construction:
the live instance simply stops adopting origin's template and keeps running the
last composed file it liked.

Third: the singular-operator decision inherits AgDR-048's weakest point whole. If
that record's single-operator premise is ever revisited, this field's shape has
to be revisited with it, and the failure mode of forgetting is a comma-bearing
value that loads clean and matches nobody.
