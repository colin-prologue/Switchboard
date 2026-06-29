# `self/` — Switchboard dogfooding its own development

This directory is the **dogfood scope**: it holds everything that Switchboard
generates when it manages *its own* development as a registered project, kept out
of the general-purpose methodology at the repo root.

The repo plays two roles, and they must not share a namespace:

- **Product role** (root: `spec/`, `workflow/`, `methodology/`, `hooks/`,
  `scripts/`) — the generic thing every registered project consumes. Reference
  material. It never mentions Switchboard's own tickets or ADRs.
- **Dogfood role** (here, `self/`) — this repo as just another project. Its
  product-intent files, decision records (ADRs about building Switchboard itself),
  and workspace conventions live here.

```
self/
  .switchboard/intents/   # product-intent files for Switchboard's own work
  .decisions/             # ADRs: "why we built Switchboard this way"
```

The hard rule: **`methodology/` never references `self/`.** If a `self/`-specific
detail leaks into the base methodology, that's content/context conflation one
level up — fix it there. `self/.decisions/` answers "why we built the tool this
way"; `methodology/` is the shipped contract for how the tool works. Different
directories so the corpus never blends the two.

Registered with: `scripts/register-project.sh --self --repo <you>/switchboard`,
which roots this project's conventions here. Doing so makes **this repo its own
first registered project** — the safest possible end-to-end test of the
register → run → pick-up-an-issue → open-a-PR loop.
