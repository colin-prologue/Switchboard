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
  # A RELATIVE workspace root in the tracked file anchored to
  # projects/<slug>/ (Config.workspace_root resolves against the workflow
  # file's parent); the composed copy lives under .run/<slug>/, so seeding
  # the literal bytes would silently move every issue workspace (codex
  # review, PR #140). Re-anchor the exact generated form on the way through.
  local rel
  rel="$(sed -n 's/^SB_WORKSPACE_ROOT=//p' "$ENV_FILE" 2>/dev/null | head -1)"
  local ok=1
  case "$rel" in
    ""|/*|"~"*|"\$"*)
      cp "$TRACKED_WF" "$tmp" 2>/dev/null || ok=0 ;;
    *)
      sed "s|root: \"$(printf '%s' "$rel" | sed -e 's/[&|\\]/\\&/g')\"|root: \"$(printf '%s' "$SB_HOME/projects/$SLUG/$rel" | sed -e 's/[&|\\]/\\&/g')\"|" \
        "$TRACKED_WF" > "$tmp" 2>/dev/null || ok=0 ;;
  esac
  if [ "$ok" = 1 ] && mv -f "$tmp" "$COMPOSED" 2>/dev/null; then
    return 0
  fi
  rm -f "$tmp" 2>/dev/null || true
}

# Set once origin/$SELF_REF has been fetched: from that point a fail-open
# skips only the RECOMPOSE — the staleness check still runs (codex review,
# PR #140: a binding/template failure must not mute the restart signal when
# the same origin update also moved orchestrator/src/).
FETCHED=0

fail_open() {
  warn "$1"
  seed_if_absent
  [ "$FETCHED" = 1 ] && check_staleness
  exit 0
}

check_staleness() {
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
if ! behind="$(git -C "$SB_HOME" rev-list --count \
      "${loaded_sha:-HEAD}..origin/$SELF_REF" -- orchestrator/src/ 2>/dev/null)"; then
  # Traversal failure (pruned loaded sha, corrupt ref) is NOT freshness:
  # deleting the marker here would report clean exactly when staleness
  # cannot be determined (codex review, PR #140). Keep whatever marker
  # exists and let a later successful traversal arbitrate.
  warn "rev-list failed measuring staleness — keeping any existing marker (fail-open)"
  return 0
fi
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
#
# The http.lowSpeed* knobs abort only a slow in-progress HTTP transfer — they
# are neither a wall-clock deadline nor applicable to SSH (codex review,
# PR #140), so the fetch also runs under a transport-independent watchdog:
# a stall in DNS, connection setup, or an SSH transport is killed after
# SB_FETCH_DEADLINE seconds and takes the same fail-open path.
fetch_with_deadline() {
  git -C "$SB_HOME" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=20 \
      fetch --quiet origin "$SELF_REF" >/dev/null 2>&1 &
  local gitpid=$!
  # the watchdog holds /dev/null, not our stdio: an orphaned sleep that
  # inherited the caller's pipe would hold it open to EOF and stall any
  # harness reading this script's output for the full deadline
  ( sleep "${SB_FETCH_DEADLINE:-30}"; kill -TERM "$gitpid" 2>/dev/null ) \
    >/dev/null 2>&1 &
  local wd=$!
  wait "$gitpid" 2>/dev/null
  local rc=$?
  kill -TERM "$wd" 2>/dev/null
  wait "$wd" 2>/dev/null || true
  return "$rc"
}

if ! fetch_with_deadline; then
  fail_open "fetch of origin/$SELF_REF failed or timed out — skipping recompose (fail-open)"
fi
FETCHED=1

# --- 2. binding --------------------------------------------------------------
[ -f "$ENV_FILE" ] \
  || fail_open "no binding at $ENV_FILE — skipping recompose (fail-open)"

p_repo="$(sed -n 's/^SB_GITHUB_REPO=//p' "$ENV_FILE" | head -1)"
p_wsroot="$(sed -n 's/^SB_WORKSPACE_ROOT=//p' "$ENV_FILE" | head -1)"
p_conv="$(sed -n 's/^SB_CONVENTION_ROOT=//p' "$ENV_FILE" | head -1)"
p_base="$(sed -n 's/^SB_BASE_BRANCH=//p' "$ENV_FILE" | head -1)"

# Stance-era values are shell-QUOTED in project.env (they may contain spaces
# and run-project.sh sources the file), so a raw sed read would keep the quotes
# and compose a workflow that differs from register-project.sh's and
# verify-setup.sh's. Read them the way both of those do — source in a subshell
# — so the quoting round-trips.
p_vcmd="$(set -a; . "$ENV_FILE" >/dev/null 2>&1; printf '%s' "${SB_VERIFY_CMD:-}")"
p_vtools="$(set -a; . "$ENV_FILE" >/dev/null 2>&1; printf '%s' "${SB_VERIFY_TOOLS:-}")"
p_rbot="$(set -a; . "$ENV_FILE" >/dev/null 2>&1; printf '%s' "${SB_REVIEW_BOT:-}")"
p_rbot_yaml=""; [ -n "$p_rbot" ] && p_rbot_yaml="\"$p_rbot\""

# SB_WORKFLOW_STANCE supersedes the legacy SB_WORKFLOW_TEMPLATE; read both, in
# that order, exactly as verify-setup.sh does. A project registered before the
# stance ladder keeps working, and — because neither key nor any other value is
# newly required — this routine adopts origin for projects registered BEFORE
# #32 landed, with no re-registration. That is the ticket's own point: a
# freshness mechanism that only works after you re-run the thing you were
# trying to stop re-running is not a freshness mechanism.
p_template="$(sed -n 's/^SB_WORKFLOW_STANCE=//p' "$ENV_FILE" | head -1)"
[ -n "$p_template" ] || p_template="$(sed -n 's/^SB_WORKFLOW_TEMPLATE=//p' "$ENV_FILE" | head -1)"

# The template is read from ORIGIN, but the values bound into it are read from
# the LOCAL tracked project.env — the same asymmetry verify-setup.sh already
# has. An origin-side binding change is therefore not adopted without a pull;
# stated, accepted, and shared with that check.
#
# Absent stance defaults to 'base', mirroring verify-setup.sh so this routine
# and the CI drift check agree. max_concurrent_agents is NOT a project.env key:
# it is read back out of the tracked composed workflow, again mirroring
# verify-setup.sh. There is no safe default for it — emitting a literal
# {{MAX_AGENTS}} would hand the loader a corrupt workflow — so an unreadable
# value skips the whole recompose instead.
p_template="${p_template:-base}"
p_agents="$(sed -n 's/^  max_concurrent_agents: \([0-9][0-9]*\)$/\1/p' "$TRACKED_WF" 2>/dev/null | head -1)"

[ -n "$p_agents" ] \
  || fail_open "cannot read max_concurrent_agents from $TRACKED_WF — skipping recompose (fail-open)"
[ -n "$p_repo" ] \
  || fail_open "SB_GITHUB_REPO missing from $ENV_FILE — skipping recompose (fail-open)"
[ -n "$p_wsroot" ] \
  || fail_open "SB_WORKSPACE_ROOT missing from $ENV_FILE — skipping recompose (fail-open)"
# A relative root historically anchored to projects/<slug>/ (the tracked
# workflow's parent); the composed file lives under .run/<slug>/, so the
# value is absolutized against the ORIGINAL anchor before substitution —
# otherwise existing issue workspaces would be abandoned and recreated under
# .run (codex review, PR #140).
case "$p_wsroot" in
  /*|"~"*|"\$"*) ;;  # absolute, home-anchored, or variable-based ($HOME/…):
                     # Config._expand_path expands $VAR before the
                     # absoluteness test (codex review, PR #140)
  *) p_wsroot="$SB_HOME/projects/$SLUG/$p_wsroot";;
esac
# p_conv is legitimately empty for root projects — never a completeness failure.

# Stance/template resolution, mirroring verify-setup.sh's ladder: the three
# legacy names map to fixed paths, anything else is a stance recipe with a
# project-local override winning over the shared ladder.
#
# Existence is probed at ORIGIN, not in the working tree. verify-setup.sh tests
# the local tree because it is checking the local tree; this routine composes
# from origin, so a local-only override would resolve to a path `git show`
# cannot read and fail open one step later with a misleading "template absent"
# — while an override that exists at origin but not yet locally is exactly the
# case this ticket exists to adopt.
case "$p_template" in
  base)          TEMPLATE="workflow/WORKFLOW.base.md";;
  codex-canary)  TEMPLATE="workflow/WORKFLOW.codex-canary.md";;
  mixed-canary)  TEMPLATE="workflow/WORKFLOW.mixed-canary.md";;
  *)
    TEMPLATE="projects/$SLUG/WORKFLOW.$p_template.md"
    git -C "$SB_HOME" cat-file -e "origin/$SELF_REF:$TEMPLATE" 2>/dev/null \
      || TEMPLATE="workflow/stances/WORKFLOW.$p_template.md"
    ;;
esac

# --- 3. recompose from origin ------------------------------------------------
# An ABSENT template file at origin is a distinct state from an absent key.
if ! raw="$(git -C "$SB_HOME" show "origin/$SELF_REF:$TEMPLATE" 2>/dev/null)"; then
  fail_open "template $TEMPLATE absent at origin/$SELF_REF — skipping recompose (fail-open)"
fi

# sed replacement text treats & \ and the delimiter specially: a checkout
# path like /srv/R&D would re-inject the pattern, and a | would abort the
# substitution (codex review, PR #140) — escape every interpolated value.
sed_escape() { printf '%s' "$1" | sed -e 's/[&|\\]/\\&/g'; }

if ! composed="$(printf '%s\n' "$raw" | sed \
  -e "s|{{REPO}}|$(sed_escape "$p_repo")|g" \
  -e "s|{{WORKSPACE_ROOT}}|$(sed_escape "$p_wsroot")|g" \
  -e "s|{{MAX_AGENTS}}|$(sed_escape "$p_agents")|g" \
  -e "s|{{CONVENTION_ROOT}}|$(sed_escape "$p_conv")|g" \
  -e "s|{{BASE_BRANCH}}|$(sed_escape "$p_base")|g" \
  -e "s|{{VERIFY_CMD}}|$(sed_escape "$p_vcmd")|g" \
  -e "s|{{VERIFY_TOOLS}}|$(sed_escape "$p_vtools")|g" \
  -e "s|{{REVIEW_BOT}}|$(sed_escape "$p_rbot")|g" \
  -e "s|{{REVIEW_BOT_YAML}}|$(sed_escape "$p_rbot_yaml")|g")" || [ -z "$composed" ]; then
  fail_open "substitution failed composing $TEMPLATE — skipping recompose (fail-open)"
fi

# An unresolved scaffold placeholder means origin's template grew a token
# this older preflight cannot bind; installing it would hand the loader a
# corrupt workflow. Keep the last usable composed file instead (codex
# review, PR #140).
if printf '%s\n' "$composed" | grep -q '{{[A-Z_][A-Z_]*}}'; then
  fail_open "unresolved placeholder(s) in composed $TEMPLATE — origin template newer than this preflight; skipping recompose (fail-open)"
fi

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
# close. A function so post-fetch fail-open paths run it too (codex review,
# PR #140).

check_staleness
exit 0
