#!/usr/bin/env bash
# new-ticket.sh — file a correctly-shaped Switchboard ticket in one command.
#
# Ticket-authoring conventions (body template, entry-state label, milestone
# attachment, blocked-by chaining) live here as an executable pathway instead of
# prose, so every author — human, assistant session, and the triage verifier's
# SPLIT verdict — files the same shape headlessly.
#
# Usage:
#   scripts/new-ticket.sh --title <t> [--body-file <path>|<stdin>]
#                         [--entry {drafting|triage|todo}]   (default: triage)
#                         [--milestone <name>]               (created if absent)
#                         [--blocked-by <n>[,<n>...]]        (native dependencies)
#                         [--repo <owner/name>]              (SB_GITHUB_REPO or git remote)
#   scripts/new-ticket.sh --scaffold        # emit body skeleton to stdout, don't file
#   scripts/new-ticket.sh --dry-run ...     # print resolved payload, no network write
#
# --entry maps to the `status:<entry>` label. --dry-run and --scaffold never touch
# the network (milestone is echoed by name, blocked-by by number), so they run in
# any environment; real filing requires an authenticated `gh`.
set -euo pipefail

TITLE="" BODY_FILE="" ENTRY="triage" MILESTONE="" BLOCKED_BY="" REPO=""
SCAFFOLD=0 DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --title)      TITLE="$2"; shift 2;;
    --body-file)  BODY_FILE="$2"; shift 2;;
    --entry)      ENTRY="$2"; shift 2;;
    --milestone)  MILESTONE="$2"; shift 2;;
    --blocked-by) BLOCKED_BY="$2"; shift 2;;
    --repo)       REPO="$2"; shift 2;;
    --scaffold)   SCAFFOLD=1; shift 1;;
    --dry-run)    DRY_RUN=1; shift 1;;
    -h|--help)    sed -n '2,20p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

# --- scaffold: emit the body skeleton and exit (no title/network needed) ------
if [ "$SCAFFOLD" -eq 1 ]; then
  cat <<'SKELETON'
## Intent

<one paragraph: what is being built and why. State the problem, not the solution.>

## Acceptance criteria

- [ ] <a check written pass/fail, eval-shaped — how a reviewer confirms done>
- [ ] <another check>

## Non-goals

- <a hard scope boundary this ticket must not cross>

## Consumers of mutated state
<!-- delete this section only if the ticket writes NO shared state: labels, issue state, workspaces, env -->

<enumerate every reader of state this ticket mutates, and how each consumes it.
e.g. a ticket that writes a `status:*` label must list the eligibility/dispatch
path, the between-turn role-pin check, and any `updatedAt` consumers.>

## Assumptions

- <something taken as given; if false, stop and flag — the ticket is void>
- Every cited mechanism carries a `file:line` verified at a named HEAD sha; uncitable claims are labeled guesses.
SKELETON
  exit 0
fi

# --- validate entry state ----------------------------------------------------
case "$ENTRY" in
  drafting|triage|todo) ;;
  *) echo "ERROR --entry must be one of: drafting, triage, todo (got '$ENTRY')" >&2; exit 2;;
esac
LABEL="status:$ENTRY"
# --entry todo skips triage by design (trivial, bounded criteria — see README
# "Which entry state?"): the human filing it IS the out-of-band verification,
# so stamp the triage-PASS provenance marker the dispatch guard requires
# (issue #29 / AgDR-011). An unstamped status:todo is refused, never dispatched.
if [ "$ENTRY" = "todo" ]; then
  LABEL="$LABEL,gate:triage-passed"
fi

[ -n "$TITLE" ] || { echo "ERROR --title required" >&2; exit 2; }

# --- resolve repo (explicit flag > SB_GITHUB_REPO > git remote) --------------
if [ -z "$REPO" ]; then
  REPO="${SB_GITHUB_REPO:-}"
fi
if [ -z "$REPO" ]; then
  remote_url="$(git remote get-url origin 2>/dev/null || true)"
  # normalize git@host:owner/name.git and https://host/owner/name(.git) -> owner/name
  REPO="$(printf '%s' "$remote_url" \
    | sed -E 's#^git@[^:]+:##; s#^https?://[^/]+/##; s#\.git$##')"
fi
[ -n "$REPO" ] || { echo "ERROR could not resolve repo (pass --repo, set SB_GITHUB_REPO, or add a git remote)" >&2; exit 2; }
[[ "$REPO" == */* ]] || { echo "ERROR --repo must be owner/name (got '$REPO')" >&2; exit 2; }

# --- read body (--body-file or stdin) ----------------------------------------
if [ -n "$BODY_FILE" ]; then
  [ -f "$BODY_FILE" ] || { echo "ERROR --body-file not found: $BODY_FILE" >&2; exit 2; }
  BODY="$(cat "$BODY_FILE")"
elif [ ! -t 0 ]; then
  BODY="$(cat)"
else
  BODY=""
fi

# --- normalize blocked-by into a space-separated list of numbers -------------
BLOCKERS=""
if [ -n "$BLOCKED_BY" ]; then
  IFS=',' read -ra _parts <<< "$BLOCKED_BY"
  for n in "${_parts[@]}"; do
    n="${n//[[:space:]]/}"
    [ -n "$n" ] || continue
    [[ "$n" =~ ^[0-9]+$ ]] || { echo "ERROR --blocked-by expects issue numbers, got '$n'" >&2; exit 2; }
    BLOCKERS="${BLOCKERS:+$BLOCKERS }$n"
  done
fi

# --- dry-run: print the resolved payload, no network -------------------------
if [ "$DRY_RUN" -eq 1 ]; then
  cat <<EOF
=== DRY RUN (no network writes) ===
repo:       $REPO
title:      $TITLE
labels:     $LABEL
milestone:  ${MILESTONE:-(none)}
blocked-by: ${BLOCKERS:-(none)}
--- body ---
$BODY
EOF
  exit 0
fi

# --- real filing: from here on we touch the network --------------------------
command -v gh >/dev/null || { echo "ERROR gh CLI not found" >&2; exit 1; }

# Milestone: attach by number; create via gh api if it does not exist.
# Initialized empty so the no-milestone path has an array to expand. NOTE: the
# empty init alone is NOT enough — bash < 4.4 (incl. macOS system bash 3.2)
# treats "${arr[@]}" on an empty array as unbound under `set -u`. The call site
# below guards with "${MILESTONE_ARGS[@]+...}" to stay safe on old bash.
MILESTONE_ARGS=()
if [ -n "$MILESTONE" ]; then
  # Title goes in via env, not string interpolation — a quote in the
  # milestone name must not break (or inject into) the jq program.
  ms_number="$(MS_TITLE="$MILESTONE" gh api --paginate "repos/$REPO/milestones?state=all" \
    --jq '.[] | select(.title==env.MS_TITLE) | .number' 2>/dev/null | head -n1 || true)"
  if [ -z "$ms_number" ]; then
    echo "milestone '$MILESTONE' not found, creating..." >&2
    ms_number="$(gh api "repos/$REPO/milestones" -f title="$MILESTONE" --jq .number)"
  fi
  MILESTONE_ARGS=(--milestone "$MILESTONE")
fi

# Create the issue. gh needs a body; pass it via a temp file to preserve exact text.
tmp_body="$(mktemp)"
trap 'rm -f "$tmp_body"' EXIT
printf '%s' "$BODY" > "$tmp_body"

issue_url="$(gh issue create \
  --repo "$REPO" \
  --title "$TITLE" \
  --body-file "$tmp_body" \
  --label "$LABEL" \
  "${MILESTONE_ARGS[@]+"${MILESTONE_ARGS[@]}"}")"
echo "created: $issue_url"

new_number="${issue_url##*/}"

# Native dependencies: this issue is blocked_by each named issue.
# The endpoint takes the blocker's internal issue_id, not its number, so resolve.
for b in $BLOCKERS; do
  blocker_id="$(gh api "repos/$REPO/issues/$b" --jq .id)"
  gh api "repos/$REPO/issues/$new_number/dependencies/blocked_by" \
    -F issue_id="$blocker_id" >/dev/null \
    && echo "  blocked-by #$b" \
    || echo "  WARN failed to add blocked-by #$b" >&2
done
