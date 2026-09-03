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

   One optional key belongs here too (issue #32):

   ```bash
   SB_SELF_BASE_BRANCH=main   # optional; default `main`
   ```

   It names the branch the **runtime freshness preflight** recomposes each
   project's workflow from, and measures loaded-code staleness against. It is
   deliberately checkout-scoped rather than per-project: one checkout serves
   every registered project, so putting it in a `project.env` would re-create
   the managed-repo category error. Note the self-referential caveat — per this
   feature's own thesis, editing `app.env` does not reach an already-running
   process; the new value takes effect on the next restart.

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

> **`--stance` defaults to `prototype`, and that is the wrong stance for this
> project.** The default exists so a throwaway project starts fast; Switchboard
> is the repo that governs every other project's merge rights, so it is the one
> place the loose end of the ladder does not belong. Pass `--stance base`
> explicitly here. See the stance table below before registering anything.

**Verify:** `scripts/list-projects.sh` shows `switchboard-self`; the repo's Labels
page shows the **ten** `status:*` labels (drafting, triage, todo, in-progress,
plan-review, decision, human-review, review, blocked, parked) plus the
`gate:triage-passed` provenance marker. `verify-setup.sh` reports the project
registered and its composed `WORKFLOW.md` matching its stance's template.

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

For many projects, manage one process per project with the supervision template in
`deploy/switchboard@.service` (Linux) or the launchd template below (macOS).

---

## Stage 5b — macOS supervision (launchd)  [OPTIONAL, per project]

A foreground `run-project.sh` dies with its terminal, and its only output
surface dies with it — so you cannot tell afterwards whether it crashed or was
killed. `deploy/com.switchboard.__SLUG__.plist.template` is a per-project
LaunchAgent that fixes both: it survives the terminal and it logs to a file.

It is a **LaunchAgent**, not a LaunchDaemon, because the agent CLIs authenticate
against your logged-in user session. It is opt-in per slug; registering a
project does not start one.

### Install

Render the two placeholders, create the log directory, then load. **`mkdir -p`
is not optional — launchd will not create the directory, and a job whose log
path is unwritable fails to spawn.**

```bash
SLUG=switchboard-self

mkdir -p ~/Library/LaunchAgents               # absent on a fresh account
sed -e "s/__SLUG__/$SLUG/g" -e "s/__USER__/$(id -un)/g" \
  deploy/com.switchboard.__SLUG__.plist.template \
  > ~/Library/LaunchAgents/com.switchboard.$SLUG.plist

# Check the rendered file before loading, and fix PATH / the repo path if the
# template's defaults don't match your machine (`pwd` at the repo root,
# `command -v uv`, `echo $HOME`).
plutil -lint ~/Library/LaunchAgents/com.switchboard.$SLUG.plist

mkdir -p ~/Library/Logs/switchboard          # launchd will NOT do this for you
launchctl load ~/Library/LaunchAgents/com.switchboard.$SLUG.plist
```

Stop it with `launchctl unload ~/Library/LaunchAgents/com.switchboard.$SLUG.plist`
(or `launchctl stop com.switchboard.$SLUG` to stop the process while leaving the
job loaded). A `stop` exits cleanly with status 0 and is **not** restarted —
that is what `KeepAlive = {SuccessfulExit: false}` buys you over
`KeepAlive: true`.

Credentials are **not** in the plist. The loaded agent has no interactive shell
and therefore no exported `GITHUB_TOKEN`, so it needs a complete `SB_APP_*` set
in `~/.config/switchboard/app.env` (Stage 0b). Do not paste a token into
`EnvironmentVariables`; a successful start logs
`[run-project] App identity: <login>`, which is your proof it reached `app.env`.

### The log

```bash
tail -f ~/Library/Logs/switchboard/$SLUG.log
```

Both stdout and stderr go to that one file, on purpose: neither stream is
timestamped, so splitting them would make it impossible to line a traceback up
with the startup banner it followed. There is no rotation — the file grows
without bound, which is accepted for now (issue #12).

### Health: run the check, don't remember to grep

Supervision restarts crashes. It does not **detect** degradation, and one of the
four degraded states below is invisible to `KeepAlive` by construction. The
primary path is one mechanical check:

```bash
bash scripts/fleet-health.sh              # every registered/installed slug
bash scripts/fleet-health.sh $SLUG        # just one
```

It is an **external observer**: a standalone process that reads only the log
files, the `.run/` markers and `launchctl` state, makes no network calls, and
still produces a complete report with every orchestrator dead. Findings go to
stderr one line each — state, slug, evidence, the remedy command — and the whole
fleet result lands in `.run/fleet-health.json`. Exit status is **0 all clear, 1
at least one degraded slug**, so an interval job can escalate on the status
alone.

Install it as an interval job — cron is the one-liner:

```bash
mkdir -p ~/Library/Logs/switchboard
crontab -e
# */10 * * * * bash /path/to/switchboard/scripts/fleet-health.sh \
#   >> "$HOME/Library/Logs/switchboard/fleet-health.log" 2>&1
```

Or a LaunchAgent of its own, if you prefer launchd end to end: the same plist
shape as the per-project template above with `StartInterval` set to `600`, no
`KeepAlive`, and `ProgramArguments` of `/bin/bash` plus the script path. Note
the check runs under launchd too, and so is subject to the same class of silent
death it watches for — it cannot certify itself. Reading its log is what closes
that loop.

Ten minutes is a reasonable cadence: the rate detections need a window long
enough to contain ticks, and the check refuses to advance its own baseline
across a window shorter than a minute — so running it by hand between interval
firings never fabricates or clears a finding.

It has no production baseline yet. Every threshold is a flag
(`--crash-banners`, `--wedge-ratio`, `--marker-age-hours`, `--poll-interval-s`,
`--min-window-s`); the first false alarm or missed wedge is the signal to tune
one, not to stop running it.

### What it mechanizes: four degraded states

The greps below are what the check does, written out. Read them to understand a
finding — but the check is what actually looks.

**Restarts are unbounded.** launchd has no `MaxRestarts`; `ThrottleInterval`
only rate-limits respawns. The template sets 60s, which is ≥ the 30s poll
interval, so a crash loop can never outpace one dispatch cycle — but it will
keep going forever.

- **`CRASH-LOOP`** — the startup banner
  `[run-project] <slug> -> <repo> (workspaces: …)` repeating every ~60 seconds.
  ```bash
  grep -c '^\[run-project\] '"$SLUG"' ->' ~/Library/Logs/switchboard/$SLUG.log
  ```
  More than one line per intentional restart means the process is dying and
  being respawned. The check compares this count against its previous
  observation, because a large count on a long-lived fleet is history, not a
  loop.

- **`WEDGED` — `KeepAlive` cannot detect this one.** The scheduler swallows
  every per-tick exception by design (`scheduler.py`, *"a tick must never kill
  the service"*), so the more likely degraded state is not a crash at all: the
  process stays alive and healthy-looking while every tick fails. You get
  **one** startup banner and then `tick error` forever. No restart policy can
  see this. Grep for it — and **anchor the pattern**:
  ```bash
  grep -cE '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:]{8}Z tick error( |$)' \
    ~/Library/Logs/switchboard/$SLUG.log
  ```
  A bare substring match is wrong here: handled in-tick failures log lines like
  `candidate fetch failed; skipping dispatch this tick error="transport error:
  ReadTimeout"`, which contain the phrase and are not the record. On this
  repo's own log that substring over-counted 94 to 0. What matters is also the
  **rate**, not the count: transport flakes happen several times a day and the
  check reports them as a notice; wedged means the count grew at the tick
  cadence with no new banner.

  Restart it by hand — and note `stop` alone leaves it DOWN, because this agent
  sets `SuccessfulExit: false` (a clean stop is a successful exit, so launchd
  does not respawn it; this section says so above):
  ```bash
  launchctl stop com.switchboard.$SLUG
  launchctl start com.switchboard.$SLUG
  ```

- **`STALE-CODE`** — the freshness preflight drops
  `.run/$SLUG/restart-needed.json` when the loaded code is behind origin, and
  surfaces it at launch. A healthy long-running process never relaunches, so
  the marker can sit unread for days. The check reports a marker older than four
  hours, and cross-checks it against the `sha=` the running process stated in
  its own `orchestrator starting` record — which is the better signal, because
  it describes the code actually loaded rather than what the preflight last
  observed. Same stop-then-start remedy.

- **`DOWN`** — a loaded-but-stopped job is a durable silent state, for the same
  `SuccessfulExit: false` reason. The check compares an installed
  `~/Library/LaunchAgents/com.switchboard.$SLUG.plist` against `launchctl list`:
  ```bash
  launchctl list com.switchboard.$SLUG   # no "PID" line = loaded but not running
  ```

The check never restarts, signals or unloads anything — deliberately. The
remedy pair above has a trap (stop alone leaves it DOWN), and a degraded process
paused in place is evidence; a reflexively restarted one is evidence destroyed.

One more thing worth knowing before you restart anything: parked issues stay
parked across a restart (`status:parked` is a durable label), but the
per-issue **session counter is process memory only**, so every restart refunds
each issue's attempt budget. An issue only parks if it burns
`max_sessions_per_issue` failures inside a single process lifetime. Durable
session counts are issue #15.

---

## Stage 6 — Onboard real projects  [SCRIPT, repeat]

```bash
scripts/register-project.sh --slug acme-api --repo acme/api --base main \
  --stance prototype \
  --verify-cmd './test.sh' \
  --verify-tools '"Bash(./test.sh:*)"'
scripts/run-project.sh acme-api
```

Real projects never receive the kit — they're registered. Only `.switchboard/` /
`.decisions/` conventions and the `status:*` labels land in their repos.

### Choose the stance deliberately — it decides who merges

`--stance` is the project's discipline dial. It is **not** cosmetic: it selects
the workflow recipe, which states get dispatched, and — since `AgDR-043` — whether
an agent is permitted to merge that project's own PRs.

| Stance | Use when | What it does |
|---|---|---|
| `prototype` *(default)* | You do not know what you are building yet, and a bad merge costs one `git revert` on a repo nobody depends on | No triage, no plan gate. A QA session reviews and merges; only its escalation list reaches you |
| `harden` | *Not yet written* | — |
| `sustain` | *Not yet written* | — |
| `base` | Something outside the project depends on it | The pre-stance pipeline: triage, plan review, and a human Gate C at `status:human-review` |

**The default is `prototype`.** Registering without `--stance` gives that
project's agents the right to merge their own work. That is usually what you want
for a new prototype and never what you want for infrastructure — so decide rather
than inherit.

### `--verify-tools` is not optional in practice

`--verify-cmd` tells the worker how to check its work; `--verify-tools` grants
permission to actually run it. Without the grant the command is denied at the
tool layer and the session strands — the failure looks like the agent refusing to
test rather than like a missing permission.

**Include every command a session needs to run**, not just the test suite. A
project that captures screenshots, builds an artifact, or shells out to a
formatter needs each of those allowed. Adding one later means re-running
`register-project.sh` with the **full** flag set: omitted flags fall back to
defaults rather than being preserved, so a partial re-run silently drops your
verify command and review bot.

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
deploy/com.switchboard.__SLUG__.plist.template
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
