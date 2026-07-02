#!/usr/bin/env bash
# verify-setup.sh — report how far through SETUP.md this repo is.
# Non-destructive, read-only. Prints a checklist and your current stage.
# Exit 0 always (it's a status report, not a gate).
set -uo pipefail

SB_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SB_HOME"

ok()   { printf '  [ ok ] %s\n' "$*"; }
pend() { printf '  [pend] %s\n' "$*"; PENDING=$((PENDING+1)); }
warn() { printf '  [warn] %s\n' "$*"; }
fail() { printf '  [FAIL] %s\n' "$*"; FAILED=$((FAILED+1)); }
PENDING=0; FAILED=0

echo "Switchboard setup check  ($SB_HOME)"
echo

# --- Kit integrity ----------------------------------------------------------
echo "Kit files:"
KIT_OK=1
for f in spec/SPEC.md spec/SPEC.core.md spec/PROVENANCE.md \
         workflow/WORKFLOW.base.md methodology/METHODOLOGY.md \
         hooks/after_create.sh hooks/before_run.sh hooks/after_run.sh \
         scripts/register-project.sh scripts/run-project.sh scripts/list-projects.sh; do
  if [ -f "$f" ]; then :; else fail "missing $f"; KIT_OK=0; fi
done
for f in hooks/*.sh scripts/*.sh; do
  [ -x "$f" ] || warn "not executable: $f  (chmod +x $f)"
done
[ "$KIT_OK" -eq 1 ] && ok "all kit files present"

# --- Prereqs ----------------------------------------------------------------
echo "Prerequisites:"
command -v git   >/dev/null && ok "git present"   || fail "git not found"
command -v gh    >/dev/null && ok "gh present"     || fail "gh CLI not found"
command -v claude>/dev/null && ok "claude present" || pend "claude CLI not found (needed at runtime)"
if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  ok "gh authenticated"
else
  pend "gh not authenticated  (gh auth login && gh auth setup-git)"
fi

# --- Stage 1: repurposed ----------------------------------------------------
echo "Stage 1 — repurposed repo:"
if [ -d ARCHIVE ]; then ok "ARCHIVE/ present"; else warn "no ARCHIVE/ (fine if this was a fresh repo)"; fi

# --- Stage 2: spec vendored + provenance ------------------------------------
echo "Stage 2 — vendored spec:"
SPEC_VENDORED=0; PROV_FILLED=0
if [ -f spec/SPEC.core.md ] && ! grep -q "PASTE VENDORED SYMPHONY" spec/SPEC.core.md \
   && [ "$(wc -l < spec/SPEC.core.md)" -gt 5 ]; then
  ok "spec/SPEC.core.md vendored"; SPEC_VENDORED=1
else
  pend "spec/SPEC.core.md still has the paste marker — paste Symphony SPEC.md (Stage 2b)"
fi
if [ -f spec/PROVENANCE.md ] && ! grep -q "fill in the SHA" spec/PROVENANCE.md; then
  ok "provenance SHA filled"; PROV_FILLED=1
else
  pend "spec/PROVENANCE.md still has the placeholder SHA (Stage 2c)"
fi

# --- Stage 3: orchestrator built --------------------------------------------
echo "Stage 3 — orchestrator:"
ORCH_BUILT=0
ORCH_FILES=$(find orchestrator -type f ! -name '.gitkeep' 2>/dev/null | wc -l | tr -d ' ')
if [ "${ORCH_FILES:-0}" -gt 0 ]; then ok "orchestrator/ has $ORCH_FILES file(s)"; ORCH_BUILT=1
else pend "orchestrator/ empty — generate with Claude Code (Stage 3)"; fi
if [ -n "${SB_ORCHESTRATOR_CMD:-}" ]; then ok "SB_ORCHESTRATOR_CMD set"; else warn "SB_ORCHESTRATOR_CMD not set in this shell (needed to run)"; fi

# --- Stage 4: projects registered -------------------------------------------
echo "Stage 4 — registered projects:"
PROJ_COUNT=0
shopt -s nullglob
for env in projects/*/project.env; do
  PROJ_COUNT=$((PROJ_COUNT+1))
  slug="$(basename "$(dirname "$env")")"
  wf="projects/$slug/WORKFLOW.md"
  if [ -f "$wf" ] && grep -q '{{' "$wf"; then
    fail "$slug: WORKFLOW.md has unsubstituted {{placeholders}}"
  else
    ok "project '$slug' registered"
  fi
done
[ "$PROJ_COUNT" -eq 0 ] && pend "no projects yet — try: scripts/register-project.sh --self --repo <you>/switchboard"

# --- Stage summary ----------------------------------------------------------
echo
if   [ "$KIT_OK" -ne 1 ];                                   then STAGE="0 (kit incomplete)"; NEXT="restore missing kit files";
elif [ "$SPEC_VENDORED" -ne 1 ] || [ "$PROV_FILLED" -ne 1 ];then STAGE="2"; NEXT="vendor SPEC.core.md and fill PROVENANCE.md (SETUP Stage 2)";
elif [ "$ORCH_BUILT" -ne 1 ];                               then STAGE="3"; NEXT="generate the orchestrator with Claude Code (SETUP Stage 3)";
elif [ "$PROJ_COUNT" -eq 0 ];                               then STAGE="4"; NEXT="register the self project (SETUP Stage 4)";
else STAGE="5 — ready to run"; NEXT="export GITHUB_TOKEN + SB_ORCHESTRATOR_CMD, then scripts/run-project.sh switchboard-self"; fi

printf 'Summary: %d pending, %d failed.\n' "$PENDING" "$FAILED"
printf 'You are at: Stage %s\n' "$STAGE"
printf 'Next: %s\n' "$NEXT"
exit 0
