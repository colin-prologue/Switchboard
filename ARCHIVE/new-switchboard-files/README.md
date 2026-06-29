# Switchboard

Switchboard is no longer a framework. It is a **methodology layer expressed as
configuration on top of a vendored [Symphony](https://github.com/openai/symphony)
orchestrator** (a one-time copy, now owned — see `spec/PROVENANCE.md`), targeting
**Claude** (execution) and **GitHub Issues** (tracker).

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
methodology rides on top as `WORKFLOW.md` + repo conventions + one MCP tool. When
the orchestration substrate revises, your methodology survives.

---

## Repurposing this repo (archive the old Switchboard)

Preserve history, then clear the root for the fork. From the repo root:

```bash
# 1. Tag the legacy state so nothing is lost.
git tag switchboard-legacy-archive
git push origin switchboard-legacy-archive

# 2. Move everything that was here into ARCHIVE/ in one commit (history retained).
mkdir -p ARCHIVE
git ls-files | grep -v '^ARCHIVE/' | xargs -I{} git mv {} ARCHIVE/{} 2>/dev/null || true
git commit -m "Archive legacy Switchboard; repo becomes Symphony-fork home"

# 3. Drop this setup kit onto the clean root and commit.
```

The old framework (lanes, Jam, tier-pinned pools) lives in `ARCHIVE/` for
reference only. Nothing imports from it.

---

## Layout

```
switchboard/
  spec/
    SPEC.md               # OWNED top-level spec (Claude+GitHub bindings + extensions)
    SPEC.core.md          # one-time vendored Symphony orchestration body (paste once)
    PROVENANCE.md         # where SPEC.core.md came from; manual-upgrade notes
  orchestrator/             # the generated implementation (Phase 1) — empty until built
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
  deploy/switchboard@.service  # optional systemd template for managing the N processes
  projects/<slug>/          # per-project binding (created by register-project.sh)
    project.env
    WORKFLOW.md
  self/                     # DOGFOOD scope: this repo managed as its own project
    .switchboard/intents/   #   its product-intent files
    .decisions/             #   its ADRs ("why we built Switchboard this way")
  ARCHIVE/                  # frozen legacy Switchboard, reference only
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
- The **orchestrator** (Phase 1): first paste the Symphony `SPEC.md` body into
  `spec/SPEC.core.md` (once) and record its origin in `spec/PROVENANCE.md`. Then
  generate the orchestrator by pointing Claude Code at `spec/SPEC.md` +
  `spec/SPEC.core.md`, and set `SB_ORCHESTRATOR_CMD` (see below). Until then,
  registration works but `run-project.sh` has nothing to launch.

---

## Onboarding a project (the answer to "do I clone the base repo?")

No. You **register** an existing repo. Greenfield or existing, same three steps:

```bash
# 1. Scaffold the binding + create gate-state labels on the repo's issue board.
scripts/register-project.sh --slug acme-api --repo acme/api --base main

# 2. Launch its process (one per project; use a process manager for N — see deploy/).
SB_ORCHESTRATOR_CMD="node orchestrator/dist/main.js" scripts/run-project.sh acme-api

# 3. File GitHub issues. The army picks them up.
```

What is **shared/installed once**: orchestrator, both adapters, corpus engine.
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

## What runs today vs. after Phase 1

- **Today:** archive + restructure, `register-project.sh` (bindings + labels +
  composed `WORKFLOW.md`, including `--self`), the hooks, the methodology config.
- **After Phase 1** (orchestrator generated from `spec/SPEC.md` + `spec/SPEC.core.md`):
  `run-project.sh` has a binary to launch and the loop goes live.

See `spec/SPEC.md` for the bindings and `methodology/METHODOLOGY.md` for the
gate-state workflow and proportionality rules.
