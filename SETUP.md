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

It prints a checklist and a single "You are at: Stage N — next: …" line.

**State of THIS repo:** Stages 0–5 are done (the verifier should report
"Stage 5 — ready to run"). Stages 1–3 below are kept as the historical record
and as the runbook for standing the kit up somewhere fresh.

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

## Stage 0b — Register the Switchboard GitHub App  [MANUAL]  *(~5 min, one-time — done for THIS install: `switchboard-agent`)*

The orchestrator and its agents act as a dedicated **`switchboard-agent[bot]`**
identity, not your personal account. This makes agent actions attributable,
lets you formally **approve** agent PRs (GitHub blocks approving your *own*
PRs, so a shared personal identity degrades Gate C to merge-without-review),
and gives the installation its own rate-limit budget. The App is **$0** — no
org, no seats. See `self/.decisions/AgDR-009-github-app-identity.md`.

If you'd rather not set this up yet, skip it: the kit runs on your personal
token (Stage 5's fallback). You lose real Gate-C approvals until you switch.

1. **Create the App.** github.com → Settings → Developer settings → **GitHub
   Apps** → **New GitHub App**. Name it (e.g. `switchboard-agent`). Uncheck
   **Webhook → Active** (unused). Repository permissions: **Issues** Read &
   write · **Contents** Read & write · **Pull requests** Read & write
   (Metadata Read comes automatically). Note the **App ID**.
2. **Generate a private key** (App page → Private keys) and store it as the
   ONLY secret at rest:
   ```bash
   mkdir -p ~/.config/switchboard && chmod 700 ~/.config/switchboard
   mv ~/Downloads/<app>.private-key.pem ~/.config/switchboard/switchboard-agent.pem
   chmod 600 ~/.config/switchboard/switchboard-agent.pem
   ```
3. **Install it** on your account, scoped to the repos Switchboard manages
   (this repo for dogfooding; add real repos in Stage 6). The installation id:
   `gh api /users/<you>/installation --jq .id`.
4. **Write `~/.config/switchboard/app.env`** (non-secret identifiers; the
   secret stays in the `.pem` it references) — `run-project.sh` sources this
   automatically:
   ```bash
   SB_APP_ID=<app id>
   SB_APP_INSTALLATION_ID=<installation id>
   SB_APP_PRIVATE_KEY_FILE=$HOME/.config/switchboard/switchboard-agent.pem
   SB_APP_BOT_LOGIN=<app-slug>[bot]
   SB_APP_BOT_USER_ID=<gh api '/users/<app-slug>[bot]' --jq .id>
   ```
   `chmod 600` it. All five are required: the first three drive token minting
   (a partial set fails startup loudly — no silent fallback to your personal
   identity), the last two set the workspace git identity
   (`<id>+<app-slug>[bot]@users.noreply.github.com`).

**Verify:** the App page shows the three permissions; the `.pem` and `app.env`
are `chmod 600`; launching (Stage 5) logs `App identity: <app-slug>[bot]`.

---

## Stage 1 — Repurpose the repo  [MANUAL]  *(done — historical record)*

**What was actually done here** (differs from the originally drafted runbook):
the legacy state was preserved in git only — tag `switchboard-legacy-archive`
plus branches `archive/main` and `archive/switchboard-v2` — and `main` was
**reset** for the fork. No `ARCHIVE/` directory exists in the working tree.

For a fresh repo: skip this stage entirely. For repurposing another existing
repo: tag it, branch the old state aside (`git branch archive/main`), reset or
orphan a new `main`, and drop the kit files onto the clean root. (Do not bulk
`git mv` into an archive subdirectory — `git mv` does not create destination
parents, so that fails half-way on any nested tree.)

**Verify:** the kit files (`spec/`, `workflow/`, `hooks/`, `scripts/`, …) sit at the
repo root. `verify-setup.sh` reports "all kit files present" and finds the
legacy-archive tag.

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

## Stage 3 — Generate the orchestrator  [CLAUDE CODE]  *(this is Phase 1 — done: Python/asyncio in `orchestrator/`)*

From inside the repo, run Claude Code and give it roughly this:

> Implement the orchestrator defined by `spec/SPEC.md` and `spec/SPEC.core.md` into
> the `orchestrator/` directory. Target **<TypeScript|Python>**. Honor the Claude
> execution binding and GitHub tracker binding in `spec/SPEC.md` (these override
> the vendored core where they disagree). The binary must accept `--workflow <path>`
> and load `WORKFLOW.md` from it. Implement the workspace-population step by invoking
> the existing `hooks/` scripts. Build it and tell me the exact launch command.

Then capture the launch command it gives you. For this repo's implementation:

```bash
export SB_ORCHESTRATOR_CMD="uv run --project orchestrator python -m orchestrator"
```

**Verify:** `orchestrator/src/orchestrator/` has the source, the test suite
passes (`uv run --project orchestrator python -m pytest orchestrator/tests -q`),
and `$SB_ORCHESTRATOR_CMD --help` runs. `verify-setup.sh` reports the
orchestrator source present.

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
page shows the **seven** `status:*` labels (drafting, triage, todo, in-progress,
plan-review, human-review, blocked). `verify-setup.sh` reports the project
registered and its composed `WORKFLOW.md` matching the current base.

---

## Stage 5 — Go live  [RUNTIME]

**Preferred — GitHub App identity (Stage 0b).** With
`~/.config/switchboard/app.env` in place there is nothing to export beyond the
orchestrator command: `run-project.sh` sources the App credential set, the
orchestrator mints short-lived (1 h) installation tokens from the `.pem` and
injects a fresh one into every tracker call, agent turn, and `git push` — no
long-lived token at rest, hourly expiry handled transparently (re-mint before
the boundary; 401 → re-mint + retry once):

```bash
export SB_ORCHESTRATOR_CMD="…"          # from Stage 3
scripts/run-project.sh switchboard-self
```

**Fallback — personal token (dogfood).** No App yet? Export a static token;
actions attribute to your account and Gate C degrades to merge-without-review
(you can't approve your own PRs):

```bash
export GITHUB_TOKEN="$(gh auth token)"
export SB_ORCHESTRATOR_CMD="…"          # from Stage 3
scripts/run-project.sh switchboard-self
```

Then file a small test ticket — `scripts/new-ticket.sh --title "..."` gives it
the right body shape and the default `status:triage` entry label (the triage
verifier promotes it to `status:todo` on PASS; add `--entry todo` to skip the
gate) — and watch it get picked up → a PR opened → the issue moved to
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

After Stage 4 (Phase-1 build done, self registered), the tracked tree
(`git ls-files`) should look like this:

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
scripts/new-ticket.sh
scripts/verify-setup.sh
deploy/switchboard@.service
orchestrator/pyproject.toml
orchestrator/uv.lock
orchestrator/src/orchestrator/…   # scheduler, runner, tracker, workspace, …
orchestrator/tests/…              # pytest suite
projects/switchboard-self/project.env
projects/switchboard-self/WORKFLOW.md
self/README.md
self/.switchboard/intents/…       # product-intent files as they accrue
self/.decisions/…                 # ADR-000 + AgDR records
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
