# AgDR-2026-08-29 — Disabling defaults are declared in a tracked sidecar, and only a project can claim one

- **Status:** proposed (ratify at this PR's merge gate)
- **Issue:** #172
- **Date:** 2026-08-29
- **Relates to:** applies **AgDR-2026-08-29-login-config-is-a-project-binding**
  (#171), which fixed the two known instances this audit now watches for a
  third. Extends **AgDR-041** (the runtime freshness preflight) with a second
  advisory report on the same launch path. Narrows nothing in **AgDR-037**:
  taking the review-response loop live stays a deliberate operator act; this
  record only decides who may *silence the reminder* that it is not live.

## Context

Three features have shipped green, reviewed and merged — and never executed.
The board-state sync Action was staged in a directory nothing reads. Fold
detection and the review-response loop each short-circuit on an empty allowlist,
so both were inert for months at zero API cost and zero log output. Each was
found by tripping over it: a `/fold` sitting dead for 25 minutes, a Codex thread
answered by hand.

The failure is structural rather than careless. **Tests exercise the feature,
not its enablement.** A unit test constructs a config with `bot_logins`
populated and asserts the loop behaves; nothing asserts that any *real* project
ever populates it. Enabling is a human action outside the test surface, and
nothing in this system tracks human actions.

The third instance was found by a one-line `grep` for disabling defaults over
one composed workflow — an unknown instance on the check's first manual run.
The discovery rate is not theoretical, and the cost is a file read.

## Decision

### 1. The policy is data in a tracked sidecar, not prose and not inference

`workflow/disabling-defaults.yml` carries both halves of the policy: the field
path → off-value table, and the per-project exemptions. Nothing scans for
empty-looking values. A disabling default is a *documented* property of a field
— `fold.operator_logins: []` is a short-circuit somebody wrote on purpose — not
a shape a linter can recognise. The table therefore admits non-empty off-values
(`mode: "never"`) that no empty-scan could ever find, and stays silent about the
many legitimately-empty fields that are not switches at all.

A **sidecar** rather than an annotation beside the field it annotates, and this
is forced rather than stylistic: `workflow.py` raises `workflow_parse_error` on
any key under `fold:` or `review_response:` other than the single known one, so
an inline annotation would make every composed config unloadable. The sidecar
needs no parser change and has exactly one reader.

### 2. Deliberately-off is a per-project assertion; template prose is not an exemption

`workflow/WORKFLOW.base.md` calls `review_response.bot_logins` "SHIPPED EMPTY ON
PURPOSE". That sentence documents the value every project *starts from* — the
template's default — and is explicitly not a decision any project has made.
Only a `deliberately_off:` entry keyed by project slug silences the check.

Without this rule the headline instance is simultaneously the thing the check
must report and the thing it must ignore, and the check reports nothing on the
one case that proved it works. The general form: **a default is not a decision,
and only the party who would live with the consequence may declare it one.**

### 3. The check runs from the launch path, not only from pytest

A pytest-only check would itself be a shipped feature that never runs against
real bytes — the exact failure this ticket exists to end, reproduced by its own
fix. So `scripts/freshness-preflight.sh` invokes it for the slug it just
recomposed. That script is the only existing path holding the composed bytes at
the moment it produces them, and it already runs once per project per launch.
Findings go to its existing stderr warning channel: advisory, fail-open, never
an exit code. A gate here would let an audit nobody asked for refuse a launch.

It audits the **composed** config, never the tracked template, because
composed-vs-tracked is itself one of the ways a feature ends up unwired.

### 4. The checker is stdlib-only, run under a bare `python3`

The preflight runs before `run-project.sh` execs the orchestrator, so no
virtualenv is guaranteed to exist. Importing `yaml` would make the common
outcome "the audit silently never ran" — again the bug class, reproduced by the
fix. The cost is a small strict value reader that understands scalars, flow
sequences and block sequences and *raises* on anything else. It never parses the
whole front matter: it walks only the lines on the requested path, because the
real front matter carries block scalars under `hooks:` that no subset reader
should try to hold.

### 5. An absent field is reported, not ignored

Deleting the `fold:` block is the same silence as emptying its list, so it earns
the same finding. Strict value-equality alone would let the audit go quiet
exactly when a feature disappears from the config altogether.

## Rejected options, steelmanned

**Annotate the field in place (`fold: { operator_logins: [], deliberately_off: true }`).**
The strongest option on locality grounds — a reader of the config would see the
claim beside the value, and the sidecar's field paths can drift from the fields
they name with nothing to catch it. Rejected because `workflow.py` rejects
unknown keys in both blocks, so this needs a parser change to a loader whose
strictness is load-bearing elsewhere; and because the annotation would then live
in a *template* that every project shares, which is precisely the conflation
decision 2 exists to prevent.

**Infer disabling defaults by scanning for empty values.** Cheaper, needs no
data file, and is exactly the `grep` that found the third instance. Rejected
because it cannot express `mode: "never"`, and because its false-positive rate
over a real front matter is high enough to get the whole report muted — and a
muted check is worse than no check, which is this ticket's own stated risk.

**Run the check in the scheduler instead, on every poll.** It would notice a
mid-flight config edit that a launch-time check cannot. Rejected as
disproportionate: the value being watched changes at most a few times a year and
only by an operator edit, so per-poll evaluation buys latency nobody needs and
adds a warning surface to the hot loop.

**Depend on pyyaml via `uv run --project orchestrator`.** Reuses the loader the
orchestrator itself uses, so the audit and the runtime could never disagree
about what a field means. Rejected because it makes an interpreter bootstrap a
precondition of the audit running at all, and its failure mode is silent
skipping — see decision 4. The disagreement risk is real and accepted, bounded
by the reader raising rather than guessing.

**Block the launch on a finding.** Rejected outright, consistent with #52's
board check: detect, never revert. `freshness-preflight.sh` runs under
`run-project.sh`'s `set -euo pipefail`, where a non-zero "fail-open" path is a
hard launch refusal — i.e. not fail-open at all.

## Blast radius

One new tracked file with one reader. `workflow/disabling-defaults.yml` does not
participate in composition, is not substituted by `scripts/register-project.sh`,
and is not read by `workflow.py`, so it cannot drift the template-drift check in
`scripts/verify-setup.sh`. The new module is imported by nothing in the
orchestrator's runtime path. The preflight gains one subprocess per project per
launch and one warning channel it already had. No tracker state, no labels, no
env, no config is written by any of it.

The one behaviour change outside this ticket's surface: a launch whose composed
config has an unwired feature now prints warnings where it previously printed
none. That is the ticket.

## Weakest point

**The table is a second source of truth about what "off" means, and nothing
binds it to the code that implements the short-circuit.** If somebody changes
`review_response` so that an empty list means "allow every bot" rather than
"disabled", the table keeps reporting the old meaning and the audit becomes
confidently wrong — a worse state than silence, because a wrong report is still
read as a report. Nothing here detects that; the field path would still resolve
and the value would still match.

This is the same second-reader failure the methodology names as the signature
defect of this project, and this record does not fix it — it adds a reader. The
falsifiable claim is that a table of two entries, each citing the issue that
created it, is cheap enough to re-read when the feature changes. If the table
reaches a size where nobody re-reads it on a config change, that claim has
failed, and the remedy is to bind each entry to the short-circuit it describes
(a test that asserts the declared off-value actually disables the feature)
rather than to grow the table further.

**Second, smaller:** the value reader is a subset parser, and a composed
workflow that expresses one of these fields in a form it does not understand
raises rather than reporting — one stderr line among several. Loud enough to
find, quiet enough to ignore for a while.
