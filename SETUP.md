# SETUP.md — Switchboard setup runbook

Follow this top to bottom. Every step is tagged so you know who acts:

- **[MANUAL]** — you run it by hand (git, copy/paste, editing a file)
- **[SCRIPT]** — a kit script does it
- **[CLAUDE CODE]** — hand off to a coding agent
- **[RUNTIME]** — sets environment variables / launches a process

At any point, run the verifier to see where you are and what's left:

```bash
bash scripts/verify-setup.sh
```

It prints a checklist and a single "You are at: Stage N — next: …" line. Nothing
below is irreversible except the Stage 1 git history rewrite of the *old* repo
contents (and even that is preserved by the tag), so go at your own pace.

---

## Stage 0 — Prerequisites  [MANUAL]

Install and authenticate the tools the kit depends on:

```bash
# git and the GitHub CLI must exist; then:
gh auth login            # authenticate gh to GitHub
gh auth setup-git        # let git clone/fetch/push github.com via gh credentials
claude --version         # the Claude CLI must be on PATH (execution adapter)
```

**Verify:** `gh auth status` shows you logged in; `claude --version` prints a
version. (`verify-setup.sh` checks these.)

---

## Stage 1 — Repurpose the repo  [MANUAL]

In the repo you're turning into the Switchboard home. **Skip the archive block if
this is a brand-new empty repo.**

```bash
# 1a. Preserve the old state immovably.
git tag switchboard-legacy-archive
git push origin switchboard-legacy-archive

# 1b. Move the old contents into ARCHIVE/ in one commit (history retained).
mkdir -p ARCHIVE
git ls-files | grep -v '^ARCHIVE/' | xargs -I{} git mv {} ARCHIVE/{}
git commit -m "Archive legacy Switchboard"

# 1c. Drop this kit onto the clean root (copy the kit files in), then:
git add -A
git commit -m "Switchboard becomes Symphony-derived methodology layer"
```

**Verify:** the kit files (`spec/`, `workflow/`, `hooks/`, `scripts/`, …) sit at the
repo root; old files are under `ARCHIVE/`. `verify-setup.sh` reports "kit installed".

---

## Stage 2 — Vendor the orchestration spec  [MANUAL]

This is the SHA step you asked about. **You** copy the spec and record where it came
from — I never pulled a commit hash.

```bash
# 2a. Get the current commit SHA of the Symphony spec (no clone needed):
git ls-remote https://github.com/openai/symphony HEAD
#   -> copy the 40-char hash it prints.
#   (Confirm the repo/branch looks right when you open it in a browser.)
```

```text
2b. Open github.com/openai/symphony, copy the FULL body of SPEC.md, and paste it
    into spec/SPEC.core.md — replacing the placeholder comment entirely.

2c. Edit spec/PROVENANCE.md:
      - replace  <fill in the SHA you copied>  with the hash from 2a
      - set the date
      - confirm/adjust the license line per their LICENSE

2d. Commit:
      git add spec/ && git commit -m "Vendor Symphony orchestration spec (one-time)"
```

If you copied from the openai.com article instead of the repo, there's no commit to
cite — record the URL + date and write "copied from rendered page, no commit ref".

**Verify:** `verify-setup.sh` flips "spec vendored" and "provenance filled" to ok
(it checks that the paste-marker and the `<fill in …>` placeholder are gone).

---

## Stage 3 — Generate the orchestrator  [CLAUDE CODE]  *(this is Phase 1)*

From inside the repo, run Claude Code and give it roughly this:

> Implement the orchestrator defined by `spec/SPEC.md` and `spec/SPEC.core.md` into
> the `orchestrator/` directory. Target **<TypeScript|Python>**. Honor the Claude
> execution binding and GitHub tracker binding in `spec/SPEC.md` (these override
> the vendored core where they disagree). The binary must accept `--workflow <path>`
> and load `WORKFLOW.md` from it. Implement the workspace-population step by invoking
> the existing `hooks/` scripts. Build it and tell me the exact launch command.

Then capture the launch command it gives you:

```bash
export SB_ORCHESTRATOR_CMD="node orchestrator/dist/main.js"   # example — use yours
```

**Verify:** `orchestrator/` has real files (beyond `.gitkeep`), and
`$SB_ORCHESTRATOR_CMD --help` runs. `verify-setup.sh` reports "orchestrator built".

---

## Stage 4 — Register this repo as its own first project  [SCRIPT]

Dogfood on the safest possible target before touching anything you care about:

```bash
scripts/register-project.sh --self --repo <you>/switchboard
```

This writes `projects/switchboard-self/`, composes its `WORKFLOW.md`, scaffolds
`self/.switchboard/` + `self/.decisions/`, and creates the `status:*` labels on the
repo's issue board.

**Verify:** `scripts/list-projects.sh` shows `switchboard-self`; the repo's Labels
page shows the six `status:*` labels. `verify-setup.sh` reports "≥1 project registered".

---

## Stage 5 — Go live  [RUNTIME]

```bash
export GITHUB_TOKEN="<github app installation token>"
export SB_ORCHESTRATOR_CMD="…"          # from Stage 3
scripts/run-project.sh switchboard-self
```

Then, on the repo's issue board: open a small test issue, add the `status:todo`
label, and watch it get picked up → a PR opened → the issue moved to
`status:human-review`. That round trip is the whole loop proven end to end.

For many projects, manage one process per project with the systemd template in
`deploy/switchboard@.service`.

---

## Stage 6 — Onboard real projects  [SCRIPT, repeat]

```bash
scripts/register-project.sh --slug acme-api --repo acme/api --base main
scripts/run-project.sh acme-api
```

Real projects never receive the kit — they're registered. Only `.switchboard/` /
`.decisions/` conventions and the `status:*` labels land in their repos.

---

## Expected topology when complete

After Stage 4 (Phase-1 build done, self registered), `find . -type f` should look
like this (plus `ARCHIVE/…` and your generated `orchestrator/…` files):

```
spec/SPEC.md
spec/SPEC.core.md          # now contains the real vendored spec, not the marker
spec/PROVENANCE.md         # SHA + date filled in
workflow/WORKFLOW.base.md
methodology/METHODOLOGY.md
hooks/after_create.sh
hooks/before_run.sh
hooks/after_run.sh
scripts/register-project.sh
scripts/run-project.sh
scripts/list-projects.sh
scripts/verify-setup.sh
deploy/switchboard@.service
orchestrator/…             # generated in Stage 3 (was just .gitkeep)
projects/switchboard-self/project.env
projects/switchboard-self/WORKFLOW.md
self/README.md
self/.switchboard/intents/.gitkeep
self/.decisions/.gitkeep
README.md
SETUP.md
```

## Verify completion

Run the verifier — it checks this topology and your tool/auth state and prints a
PASS/PENDING/FAIL line per item and your current stage:

```bash
bash scripts/verify-setup.sh
```

If you'd rather have Claude check it, paste the output of
`find . -type f -not -path './.git/*' | sort` together with `bash scripts/verify-setup.sh`
and ask whether the setup is complete.
