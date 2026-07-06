# AgDR-009: GitHub App identity + secret-storage contract

- **Status:** accepted (2026-07-05). Implements issue #10.
- **Context:** Switchboard ran on the operator's personal token (`gh auth
  token`). Three costs: agent actions attribute to the operator (provenance
  lies), the operator cannot formally Approve agent PRs (GitHub blocks
  approving your own), and every project shares one rate-limit budget. Issue
  #10's human prerequisite is done: App `switchboard-agent` (id 4225392) is
  registered and installed on this repo with contents/issues/pull_requests
  write + metadata read.

## Decision 1 — identity: a GitHub App installation, minted per process

The orchestrator authenticates as the App's installation, never as a stored
token. `orchestrator/auth.py` provides one contract, two providers:

- `StaticTokenProvider` — the dogfood fallback (`$GITHUB_TOKEN`), with a no-op
  `invalidate()`.
- `AppInstallationTokenProvider` — signs a short-lived RS256 App JWT from the
  private key, mints an installation token, caches it, re-mints 300 s before
  the hourly expiry, and re-mints on `invalidate()`.

`workflow.py:build_credentials` picks the provider: a **complete**
`SB_APP_ID/SB_APP_INSTALLATION_ID/SB_APP_PRIVATE_KEY_FILE` set builds the App
provider; none of them builds the static one; a **partial set fails startup
loudly** (`incomplete_app_credentials`) — silently falling back to the
personal token would be an unnoticed identity switch. The scheduler builds
ONE provider per process lifetime (mint cache survives ticks; the process is
the sole minting authority) and threads it everywhere:

- **Tracker:** token fetched per GraphQL request; on 401 → `invalidate()` +
  retry exactly once (recovers the expiry-boundary race; never loops).
- **Agent turns:** a fresh (cached) mint is injected per turn as
  `GITHUB_TOKEN` + `GH_TOKEN`, so a session spanning the hourly boundary gets
  a valid token on its next turn. Mint failure → `WorkerFailure` (retry with
  backoff), never an agent launched without credentials.
- **Git:** `before_run.sh` sets the workspace identity to
  `<SB_APP_BOT_USER_ID>+<SB_APP_BOT_LOGIN>@users.noreply.github.com` and a
  repo-local credential helper (`username=x-access-token`,
  `password=$GITHUB_TOKEN` — single-quoted, resolved at push time in the
  agent's env), so commits author as and pushes authenticate as the bot.

## Decision 2 — secret-storage contract

- The **only secret at rest is the App private key**:
  `~/.config/switchboard/switchboard-agent.pem`, `chmod 600`, outside the
  repo. It is read once at startup, by path.
- `~/.config/switchboard/app.env` holds the **non-secret identifiers**
  (`SB_APP_*`), also `chmod 600`, sourced+exported by `run-project.sh` so the
  orchestrator (provider construction) and hooks (git identity) see them.
- Installation tokens (≤1 h) live **only in process memory and per-turn agent
  env** — never on disk, never in config, never logged (the tracker's
  no-token-in-errors posture, core §11.4, is preserved; a failed mint is
  reported by exception class name only).
- Nothing App-related lives in the repo or `project.env`; per-machine
  credentials stay per-machine.

## Weakest points (accepted)

- **Agent can read its own token** (`printenv`): inherent to giving the agent
  push rights; bounded by ≤1 h expiry and the installation's repo scoping.
- **Orchestrator-side git ops still ride ambient credentials on private
  repos:** `after_create.sh` clone / `before_run.sh` fetch run in the
  orchestrator env (no minted token injected there yet). On this machine the
  operator's `gh auth setup-git` covers them; a fresh deploy of a private
  project would need that, or a follow-up injecting the token into hook env.
- **Provider is process-lifetime:** a workflow hot-reload does not rebuild it
  (credentials are env-scoped, not workflow-scoped); changing `app.env`
  requires a restart.
- The two live-App checks (bot authors a PR; operator can Approve it) are a
  one-time post-merge validation run by the operator — not worker-checkable.
