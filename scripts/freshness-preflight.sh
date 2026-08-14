#!/usr/bin/env bash
# freshness-preflight.sh — runtime freshness for ONE project's orchestrator
# process (issue #32).
#
# Two jobs, both non-mutating with respect to the checkout:
#
#   1. RECOMPOSE config from origin. The per-project workflow template is read
#      straight out of `origin/<ref>` (never the working tree) and rendered into
#      the gitignored, per-project `$SB_HOME/.run/<slug>/composed-WORKFLOW.md`.
#      The orchestrator's mtime-driven hot reload picks it up with no restart.
#   2. SIGNAL stale CODE. Python under `orchestrator/src/**` is already loaded
#      into the long-running process; adopting it requires an operator restart.
#      When origin has moved ahead of the sha this process loaded, drop a
#      `restart-needed.json` marker. We surface it; we never act on it.
#
# What this script deliberately does NOT do: pull, merge, checkout, restart, or
# write any tracked file. HEAD is never moved. It also does not speak to
# workspace-branch staleness — that is #57's `.sb-staleness` contract.
#
# Usage:  scripts/freshness-preflight.sh <slug>
#
# Exit codes: 0 on success AND on every fail-open path (warnings go to stderr).
# Non-zero is reserved for usage errors. This runs under run-project.sh's
# `set -euo pipefail`, so a non-zero fail-open would be a hard launch refusal —
# i.e. not fail-open at all.
#
# Every git invocation and every .run/ path is anchored to $SB_HOME, never to
# CWD: at launch this runs *before* run-project.sh pins the cwd, so CWD is
# arbitrary by that script's own documentation.

set -uo pipefail

warn() { echo "[freshness] $*" >&2; }

SLUG="${1:-}"
[ -n "$SLUG" ] || { echo "usage: scripts/freshness-preflight.sh <slug>" >&2; exit 2; }

# Mirror run-project.sh:16 — derive SB_HOME from our own location when unbound.
if [ -z "${SB_HOME:-}" ]; then
  SB_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

# Never block on a credential or passphrase prompt: this runs unattended under
# the scheduler and semi-attended at launch.
export GIT_TERMINAL_PROMPT=0

RUN_DIR="$SB_HOME/.run/$SLUG"
COMPOSED="$RUN_DIR/composed-WORKFLOW.md"
MARKER="$RUN_DIR/restart-needed.json"
ENV_FILE="$SB_HOME/projects/$SLUG/project.env"
TRACKED_WF="$SB_HOME/projects/$SLUG/WORKFLOW.md"

mkdir -p "$RUN_DIR" 2>/dev/null || true

# Fallback is CONTENT-level and INITIALIZATION-ONLY. Seeding an EXISTING
# composed file would bump its mtime, parse cleanly, and silently revert the
# running process from origin's config back to the committed snapshot — a
# silent un-adoption, the exact inverse of this ticket's bug class. So a
# transient fetch failure leaves both content and mtime untouched.
seed_if_absent() {
  [ -f "$COMPOSED" ] && return 0
  [ -f "$TRACKED_WF" ] || return 0
  local tmp="$COMPOSED.tmp.$$"
  if cp "$TRACKED_WF" "$tmp" 2>/dev/null && mv -f "$tmp" "$COMPOSED" 2>/dev/null; then
    return 0
  fi
  rm -f "$tmp" 2>/dev/null || true
}

fail_open() {
  warn "$1"
  seed_if_absent
  exit 0
}

# --- 1. reachability ---------------------------------------------------------
# Checked BEFORE binding completeness so a repo-less deploy deterministically
# reports the missing-repo cause rather than an incidental missing-key one.
git -C "$SB_HOME" rev-parse --git-dir >/dev/null 2>&1 \
  || fail_open "no git repository at $SB_HOME — skipping recompose (fail-open)"

git -C "$SB_HOME" remote get-url origin >/dev/null 2>&1 \
  || fail_open "no 'origin' remote at $SB_HOME — skipping recompose (fail-open)"

# Checkout-scoped, never per-project: one checkout serves every project, so a
# per-project value would re-create the managed-repo category error.
SELF_REF="${SB_SELF_BASE_BRANCH:-main}"

# Bounded so a hung transport cannot wedge the tick. Concurrent-fetch
# collisions (three processes, one .git, staggered clocks) are an EXPECTED
# fail-open cause, not a bug.
if ! git -C "$SB_HOME" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=20 \
       fetch --quiet origin "$SELF_REF" >/dev/null 2>&1; then
  fail_open "fetch of origin/$SELF_REF failed — skipping recompose (fail-open)"
fi

# --- 2. binding --------------------------------------------------------------
[ -f "$ENV_FILE" ] \
  || fail_open "no binding at $ENV_FILE — skipping recompose (fail-open)"

p_repo="$(sed -n 's/^SB_GITHUB_REPO=//p' "$ENV_FILE" | head -1)"
p_wsroot="$(sed -n 's/^SB_WORKSPACE_ROOT=//p' "$ENV_FILE" | head -1)"
p_conv="$(sed -n 's/^SB_CONVENTION_ROOT=//p' "$ENV_FILE" | head -1)"
p_agents="$(sed -n 's/^SB_MAX_AGENTS=//p' "$ENV_FILE" | head -1)"
p_template="$(sed -n 's/^SB_WORKFLOW_TEMPLATE=//p' "$ENV_FILE" | head -1)"

# The template is read from ORIGIN, but the values bound into it are read from
# the LOCAL tracked project.env — the same asymmetry verify-setup.sh:103-106
# already has. An origin-side binding change is therefore not adopted without a
# pull; stated, accepted, and shared with that check.
#
# Absent SB_WORKFLOW_TEMPLATE defaults to 'base' (mirroring verify-setup.sh:107)
# so this routine and the CI drift check agree. Absent SB_MAX_AGENTS is a
# different animal: there is no safe default, and emitting a literal
# {{MAX_AGENTS}} would hand the loader a corrupt workflow. Skip the whole
# recompose instead.
p_template="${p_template:-base}"

[ -n "$p_agents" ] \
  || fail_open "SB_MAX_AGENTS missing from $ENV_FILE — skipping recompose (fail-open)"
[ -n "$p_repo" ] \
  || fail_open "SB_GITHUB_REPO missing from $ENV_FILE — skipping recompose (fail-open)"
[ -n "$p_wsroot" ] \
  || fail_open "SB_WORKSPACE_ROOT missing from $ENV_FILE — skipping recompose (fail-open)"
# p_conv is legitimately empty for root projects — never a completeness failure.

case "$p_template" in
  base)          TEMPLATE="workflow/WORKFLOW.base.md";;
  codex-canary)  TEMPLATE="workflow/WORKFLOW.codex-canary.md";;
  mixed-canary)  TEMPLATE="workflow/WORKFLOW.mixed-canary.md";;
  *) fail_open "unknown SB_WORKFLOW_TEMPLATE '$p_template' in $ENV_FILE — skipping recompose (fail-open)";;
esac

# --- 3. recompose from origin ------------------------------------------------
# An ABSENT template file at origin is a distinct state from an absent key.
if ! raw="$(git -C "$SB_HOME" show "origin/$SELF_REF:$TEMPLATE" 2>/dev/null)"; then
  fail_open "template $TEMPLATE absent at origin/$SELF_REF — skipping recompose (fail-open)"
fi

composed="$(printf '%s\n' "$raw" | sed \
  -e "s|{{REPO}}|$p_repo|g" \
  -e "s|{{WORKSPACE_ROOT}}|$p_wsroot|g" \
  -e "s|{{MAX_AGENTS}}|$p_agents|g" \
  -e "s|{{CONVENTION_ROOT}}|$p_conv|g")"

# Content identity, NOT hand-edit detection: the comparison is against this
# routine's own freshly composed bytes, never the tracked blob. So every origin
# change is adopted on the first pass, and a hand-edited composed file is
# overwritten because its bytes differ — a silent refusal to adopt origin is
# this ticket's own bug class.
#
# The rename happens ONLY when the bytes differ. Renaming unconditionally would
# defeat scheduler.py:368's mtime guard and log ~864 spurious "workflow
# reloaded" lines/day across three processes, inverting the very signal this
# ticket exists to keep loud.
if [ -f "$COMPOSED" ] && printf '%s\n' "$composed" | cmp -s - "$COMPOSED"; then
  : # byte-identical — leave content AND mtime alone
else
  tmp="$COMPOSED.tmp.$$"
  # Temp-then-rename: a reader sampling the path mid-write must observe either
  # the old complete file or the new one, never a partial parse (a torn read
  # sets _workflow_broken and blocks dispatch until the next tick).
  if printf '%s\n' "$composed" > "$tmp" 2>/dev/null && mv -f "$tmp" "$COMPOSED" 2>/dev/null; then
    :
  else
    rm -f "$tmp" 2>/dev/null || true
    fail_open "could not write $COMPOSED — skipping recompose (fail-open)"
  fi
fi

# --- 4. loaded-code staleness ------------------------------------------------
# Measured from the sha the PROCESS LOADED, not from HEAD. A HEAD-based
# detector self-clears on the operator's own `git pull` while the process is
# still running pre-pull code — precisely the blind spot this signal exists to
# close.
if [ -n "${SB_LAUNCH_SHA:-}" ]; then
  loaded_sha="$SB_LAUNCH_SHA"
  sha_source="launch-sha"
else
  sha_source="HEAD-fallback"
  warn "SB_LAUNCH_SHA unbound — measuring staleness from HEAD instead (degraded)"
  loaded_sha="$(git -C "$SB_HOME" rev-parse HEAD 2>/dev/null || true)"
fi

target_sha="$(git -C "$SB_HOME" rev-parse "origin/$SELF_REF" 2>/dev/null || true)"

# ONE count subsuming both conditions: checkout-behind-origin AND
# process-behind-checkout. Pathspec-limited, so a workflow-only advance
# intentionally raises no marker — that path is config, and config recomposes.
behind="$(git -C "$SB_HOME" rev-list --count "${loaded_sha:-HEAD}..origin/$SELF_REF" \
            -- orchestrator/src/ 2>/dev/null || true)"
case "$behind" in
  ''|*[!0-9]*) behind=0;;
esac

# Per-project: "needs restart" is a property of a PROCESS, not of the checkout.
# A checkout-scoped marker reported clean for all three processes the moment
# any one of them restarted.
if [ "$behind" -gt 0 ]; then
  tmp="$MARKER.tmp.$$"
  # behind_commits is a COMMIT count (rev-list --count); no file list is
  # derivable from it, and inventing one would be fake fidelity.
  if printf '{\n  "target_sha": "%s",\n  "loaded_sha": "%s",\n  "loaded_sha_source": "%s",\n  "behind_commits": %s,\n  "written_at": "%s"\n}\n' \
       "$target_sha" "$loaded_sha" "$sha_source" "$behind" \
       "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$tmp" 2>/dev/null \
     && mv -f "$tmp" "$MARKER" 2>/dev/null; then
    warn "RESTART NEEDED for '$SLUG': loaded code is $behind commit(s) behind origin/$SELF_REF under orchestrator/src/ — see $MARKER"
  else
    rm -f "$tmp" 2>/dev/null || true
    warn "could not write $MARKER (fail-open)"
  fi
else
  rm -f "$MARKER" 2>/dev/null || true
fi

exit 0
