# Switchboard

Switchboard is no longer a framework. It is a **methodology layer expressed as
configuration on top of a vendored [Symphony](https://github.com/openai/symphony)
orchestrator** (a one-time copy, now owned — see `spec/PROVENANCE.md`), targeting
**Claude** (execution) and **GitHub Issues** (tracker).

**New here / setting up? Follow [`SETUP.md`](SETUP.md) top to bottom, and run
`bash scripts/verify-setup.sh` at any point to see which stage you're on.**

There is no Switchboard runtime. There is:

- **One installed runtime** — the Symphony-derived orchestrator + the Claude
  execution adapter + the GitHub tracker adapter (+ later, the decision-corpus
  MCP). Installed once on a devbox. Runs as **N processes, one per project**.
- **Per-project bindings** — a tiny `projects/<slug>/` directory (a `project.env`
  and a composed `WORKFLOW.md`). This is what "each project has its own Symphony"
  means: a binding, not a copy.
- **A shared worker budget** — concurrency is capped per process; the army is
  shared in spirit, partitioned by process in practice (the low-regret topology
  we chose over a multiplexing daemon).

The dependency arrow points **up**: Symphony is the substrate; the Switchboard
methodology rides on top as `WORKFLOW.md` + repo conventions (+ in a later
phase, the decision-corpus MCP tool). When the orchestration substrate
revises, your methodology survives.

---

## History: where the old Switchboard went

The legacy framework (lanes, Jam, tier-pinned pools) is **not in the working
tree**. It is preserved in git only: tag `switchboard-legacy-archive` plus the
`archive/main` and `archive/switchboard-v2` branches. `main` was reset for the
Symphony fork rather than moved under an `ARCHIVE/` directory. Nothing imports
from the legacy code.

---

## Layout

```
switchboard/
  spec/
    SPEC.md               # OWNED top-level spec (Claude+GitHub bindings + extensions)
    SPEC.core.md          # one-time vendored Symphony orchestration body (paste once)
    PROVENANCE.md         # where SPEC.core.md came from; manual-upgrade notes
  orchestrator/             # the Phase-1 implementation (Python/asyncio; built, tested)
    src/orchestrator/       #   scheduler, Claude runner, GitHub tracker, workspace mgr
    tests/                  #   pytest suite (fake tracker/runner/claude)
  workflow/WORKFLOW.base.md # shared methodology base (front-matter defaults + prompt)
  methodology/METHODOLOGY.md# the IDSD workflow the agent follows (referenced by prompt)
  hooks/                    # workspace population — the clean-checkout-per-ticket linchpin
    after_create.sh         #   clone the project repo into a fresh per-issue workspace
    before_run.sh           #   ensure the per-issue branch; sync base on first run
    after_run.sh            #   optional artifact/log capture
  scripts/
    register-project.sh     # per-project registration (binding + labels + composed WORKFLOW)
    run-project.sh          # launch ONE project's Symphony process (N-process topology)
    list-projects.sh        # list registered projects
    new-ticket.sh           # file a correctly-shaped ticket (default entry: status:triage)
    verify-setup.sh         # setup-stage checklist + composed-workflow drift check
  deploy/switchboard@.service  # optional systemd template for managing the N processes
  projects/<slug>/          # per-project binding (created by register-project.sh)
    project.env
    WORKFLOW.md
  self/                     # DOGFOOD scope: this repo managed as its own project
    .switchboard/intents/   #   its product-intent files
    .decisions/             #   its ADRs/AgDRs ("why we built Switchboard this way")
```

The product role (`spec/`, `workflow/`, `methodology/`, `hooks/`, `scripts/`) is
what registered projects consume — generic, no project-specific content. The
dogfood role lives entirely under `self/`. **`methodology/` never references
`self/`.**

---

## Prerequisites (install once)

- `git`, `bash`
- **GitHub CLI** authed with git integration:
  ```bash
  gh auth login
  gh auth setup-git     # lets git clone/fetch/push github.com via gh's credentials
  ```
  The hooks rely on `gh auth setup-git`; they do not embed tokens.
- **Claude CLI** (`claude`) available on PATH for the execution adapter.
- **uv** (Python project runner) — the orchestrator is a Python/asyncio
  implementation living in `orchestrator/` (Phase 1 is done; the spec is
  vendored and `spec/PROVENANCE.md` records its origin). Launch it via:
  ```bash
  export SB_ORCHESTRATOR_CMD="uv run --project orchestrator python -m orchestrator"
  ```

---

## Onboarding a project (the answer to "do I clone the base repo?")

No. You **register** an existing repo. Greenfield or existing, same three steps:

```bash
# 1. Scaffold the binding + create gate-state labels on the repo's issue board.
scripts/register-project.sh --slug acme-api --repo acme/api --base main

# 2. Launch its process (one per project; use a process manager for N — see deploy/).
SB_ORCHESTRATOR_CMD="uv run --project orchestrator python -m orchestrator" \
  scripts/run-project.sh acme-api

# 3. File GitHub issues (scripts/new-ticket.sh gives them the right shape).
#    The army picks them up.
```

What is **shared/installed once**: orchestrator + both adapters (the
decision-corpus engine is a later phase).
What is **per-project**: the `projects/<slug>/` binding and the `.switchboard/` /
`.decisions/` conventions inside *that project's own repo*. What is **per-ticket**:
a fresh workspace, checked out to the right repo by `hooks/after_create.sh`.

Workspace roots are namespaced per project (`<base>/<slug>/<issue-number>`)
because GitHub issue numbers collide across repos (both repos have an `#12`).

### Dogfood this repo first

Register Switchboard as its own first project to validate the whole loop on the
safest target:

```bash
scripts/register-project.sh --self --repo <you>/switchboard
```

`--self` roots this project's intents/ADRs under `self/` so its own development
tickets never pollute the general-purpose `methodology/`. See `self/README.md`.

---

## Graph review (manually-invoked analyzer)

`graph-review` is a **read-only** pass that reads the open ticket board (bodies,
comments, native `blockedBy` edges, milestones) plus recently merged PRs and
writes evidence-cited, keyed proposals to a single rolling **Graph Review** issue
— prose dependencies missing a native edge, likely-wrong milestones, merge/split
candidates, assumptions a merged PR invalidated, promotable tickets. It is
proposals-only (Phase 1): it never mutates any other ticket, and there is **no
scheduler entry** — you run it by hand.

```bash
# Preview the ledger without writing to GitHub:
uv run --project orchestrator python -m orchestrator.graph_review \
  --workflow projects/switchboard-self/WORKFLOW.md --dry-run

# Write/refresh the rolling Graph Review issue (idempotent — one issue, updated
# in place; never re-raises a key you marked accepted/dismissed):
uv run --project orchestrator python -m orchestrator.graph_review \
  --workflow projects/switchboard-self/WORKFLOW.md
```

Structural proposals (`merge`/`split`/`resequence`) pass a skeptic *refute*
sub-check (`--refute-command`, default `claude -p`) before being written;
mechanical ones skip it. See `self/.switchboard/intents/graph-review.md` and
`self/.decisions/AgDR-009-graph-review-phasing.md` for the phasing and rationale.

---

## Status

Phase 1 is complete: the orchestrator is implemented (from `spec/SPEC.md` +
`spec/SPEC.core.md`), tested, and dogfooding this repo as its own project
(`projects/switchboard-self/`). The full loop — register, file a ticket via
`new-ticket.sh`, triage verification, worker dispatch, PR handoff at the human
gate — runs today. The decision-corpus MCP remains a later phase.

See `spec/SPEC.md` for the bindings and `methodology/METHODOLOGY.md` for the
gate-state workflow and proportionality rules.
