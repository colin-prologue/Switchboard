#!/usr/bin/env bash
# fleet-health.sh — the external fleet-health observer (issue #193).
#
# One pass over every registered/installed slug: crash loops, wedged ticks,
# stale loaded code, down processes. Findings go to stderr one line each; the
# full result lands in .run/fleet-health.json. Exit 0 = all clear, 1 = at least
# one degraded slug, 2 = usage error.
#
# Deliberately a bare `python3` and not `uv run`: the check exists to be
# believable when the fleet is dead, and that includes the case where the
# project virtualenv is what broke. `orchestrator.fleet_health` imports nothing
# outside the standard library.
#
# Install it as an interval job — see SETUP.md Stage 5b.
set -uo pipefail

SB_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --repo-root is passed first so a caller can still override it (argparse takes
# the last occurrence), which is what the tests do.
exec env "PYTHONPATH=$SB_HOME/orchestrator/src${PYTHONPATH:+:$PYTHONPATH}" \
  "${SB_PYTHON:-python3}" -m orchestrator.fleet_health \
  --repo-root "$SB_HOME" "$@"
