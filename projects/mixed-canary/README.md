# Mixed-canary preflight

This binding is inert until an operator launches it. It must target only the
separate private `colin-prologue/switchboard-mixed-canary` repository; never
point it at Switchboard or another existing project.

Before creating a dispatchable issue or starting `--provider mixed`:

1. Create the private repository and grant the `switchboard-agent` GitHub App
   installation access.
2. Preview the complete label set without touching GitHub:

   ```bash
   scripts/provision-mixed-canary-labels.sh --dry-run
   ```

3. Provision the labels with an authenticated operator `gh` session:

   ```bash
   scripts/provision-mixed-canary-labels.sh
   ```

   The command is idempotent. In particular, `provider:claude` and
   `provider:codex` must exist before launch: mixed dispatch writes one of them
   before claiming an issue. The `agent:claude` and `agent:codex` labels enable
   the later explicit-assignment evidence tickets.
4. Seed the reviewed synthetic fixture and confirm its tests pass on `main`.
5. Stop. Do not launch the mixed process until the native-terminal evidence
   procedure, stop condition, and Claude-only rollback drill are reviewed.

The initial workflow remains `claude: 100, codex: 0`. Provisioning labels does
not change that routing policy, create an issue, or start a worker.
