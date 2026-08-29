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
#                         [--entry {drafting|triage|todo}]   (default: resolved per project)
#                         [--milestone <name>]               (created if absent)
#                         [--blocked-by <n>[,<n>...]]        (native dependencies)
#                         [--repo <owner/name>]              (SB_GITHUB_REPO or git remote)
#   scripts/new-ticket.sh --scaffold        # emit body skeleton to stdout, don't file
#   scripts/new-ticket.sh --dry-run ...     # print resolved payload, no network write
#
# --entry maps to the `status:<entry>` label. It is NOT a fixed default: the entry
# state is only meaningful where the target project's stance dispatches it, so
# omitting --entry resolves the project's `active_states` and picks from them, and
# refuses rather than guessing when it cannot (issue #176). --dry-run and
# --scaffold never touch the network (milestone is echoed by name, blocked-by by
# number), so they run in any environment; real filing requires an authenticated `gh`.
set -euo pipefail

TITLE="" BODY_FILE="" ENTRY="" MILESTONE="" BLOCKED_BY="" REPO=""
SCAFFOLD=0 DRY_RUN=0
# Whether --entry was given EXPLICITLY. The resolved default and an explicitly
# requested state are checked differently (an explicit state may name a GATE the
# project parks at; a default may only name a state it DISPATCHES), so a bare
# value comparison against "triage" cannot distinguish them.
ENTRY_EXPLICIT=0

while [ $# -gt 0 ]; do
  case "$1" in
    --title)      TITLE="$2"; shift 2;;
    --body-file)  BODY_FILE="$2"; shift 2;;
    --entry)      ENTRY="$2"; ENTRY_EXPLICIT=1; shift 2;;
    --milestone)  MILESTONE="$2"; shift 2;;
    --blocked-by) BLOCKED_BY="$2"; shift 2;;
    --repo)       REPO="$2"; shift 2;;
    --scaffold)   SCAFFOLD=1; shift 1;;
    --dry-run)    DRY_RUN=1; shift 1;;
    -h|--help)    sed -n '2,25p' "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

# --- scaffold: emit the body skeleton and exit (no title/network needed) ------
if [ "$SCAFFOLD" -eq 1 ]; then
  cat <<'SKELETON'
## In brief

**What this does:** <one plain sentence, no issue numbers, file paths, AgDR ids,
`status:` label names, or function/field names. If you cannot say it without
them, you do not understand the change well enough to file it yet.>

**What could be wrong:** <one assumption or decision, in "if X, then Y" shape:
name the trigger and what concretely breaks. Naming a quality is not an answer
("coverage could be broader"); naming a consequence is ("if the label API is not
read-your-writes, the read-back false-negatives and the ticket strands").>

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

# --- validate the entry state's VALUE (its fitness is checked after resolution)
if [ "$ENTRY_EXPLICIT" -eq 1 ]; then
  case "$ENTRY" in
    drafting|triage|todo) ;;
    *) echo "ERROR --entry must be one of: drafting, triage, todo (got '$ENTRY')" >&2; exit 2;;
  esac
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

# --- resolve which states the TARGET PROJECT dispatches (issue #176) ---------
# An entry state is only meaningful where the project's stance dispatches it:
# `status:triage` on a `prototype` project is neither active, nor a gate, nor
# terminal, so the ticket sits forever and the tool that filed it says nothing.
#
# Resolution mirrors `status_board.workflow_for_repo()` deliberately, step for
# step: match `SB_GITHUB_REPO` across `projects/*/project.env`, then read that
# project's COMPOSED `WORKFLOW.md`. Mirrored rather than invoked because bash
# cannot import Python and the worker allowlist admits no ad-hoc interpreter
# run; `test_new_ticket.py` pins the two implementations to the same answer for
# a real binding, so this copy cannot quietly become a SECOND repo->project map
# that disagrees with the one the scheduler dispatches from (AgDR-043).
#
# The state LIST is read, not the stance NAME: `SB_WORKFLOW_STANCE` post-dates
# several bindings (switchboard-self's has no such line) and bindings are only
# rewritten on re-registration, while `active_states` is present in every
# composed file and is indifferent to how many stances exist.
SB_HOME="${SB_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# First `<key>: [...]` flow list in the file, one item per line, unquoted.
# Matched as one line rather than YAML-parsed for the same reason
# `status_board.load_active_states` does: the uncomposed templates carry
# `{{PLACEHOLDER}}` keys that a YAML loader refuses outright.
yaml_flow_list() {
  awk -v key="$2" '
    $0 ~ "^[ \t]*" key ":[ \t]*\\[" {
      sub(/^[^[]*\[/, ""); sub(/\].*$/, "")
      n = split($0, items, ",")
      for (i = 1; i <= n; i++) {
        gsub(/^[ \t"]+/, "", items[i]); gsub(/[ \t"]+$/, "", items[i])
        if (items[i] != "") print items[i]
      }
      exit
    }
  ' "$1"
}

WORKFLOW_PATH="" PROJECT_SLUG="" RESOLVE_ERR=""
ACTIVE_STATES="" GATE_STATES=""
_want="$(printf '%s' "$REPO" | tr '[:upper:]' '[:lower:]')"
_matches=""
for _env in "$SB_HOME"/projects/*/project.env; do
  [ -f "$_env" ] || continue
  _bound="$(sed -n 's/^SB_GITHUB_REPO=//p' "$_env" | head -n1 \
    | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | tr '[:upper:]' '[:lower:]')"
  [ "$_bound" = "$_want" ] || continue
  _matches="${_matches:+$_matches }$(basename "$(dirname "$_env")")"
done
set -- $_matches
if [ "$#" -eq 0 ]; then
  RESOLVE_ERR="no projects/*/project.env binds $REPO (is it registered?)"
elif [ "$#" -gt 1 ]; then
  # Ambiguity is refused rather than resolved first-match: picking one of two
  # bindings could file against a state machine the scheduler does not use.
  RESOLVE_ERR="$# projects bind $REPO ($_matches) — the binding is ambiguous"
else
  PROJECT_SLUG="$1"
  WORKFLOW_PATH="$SB_HOME/projects/$PROJECT_SLUG/WORKFLOW.md"
  if [ ! -f "$WORKFLOW_PATH" ]; then
    RESOLVE_ERR="project '$PROJECT_SLUG' binds $REPO but has no composed WORKFLOW.md"
    WORKFLOW_PATH=""
  else
    ACTIVE_STATES="$(yaml_flow_list "$WORKFLOW_PATH" active_states | tr '\n' '|')"
    GATE_STATES="$(yaml_flow_list "$WORKFLOW_PATH" gate_states | tr '\n' '|')"
    if [ -z "$ACTIVE_STATES" ]; then
      RESOLVE_ERR="$WORKFLOW_PATH declares no tracker.active_states"
      WORKFLOW_PATH=""
    fi
  fi
fi
set --   # drop the positional-parameter scratch space

has_state() { case "|$1|" in *"|$2|"*) return 0;; *) return 1;; esac; }
list_states() { printf '%s' "${1%|}" | tr '|' ',' | sed 's/,/, /g'; }
# The subset of --entry's values this project would actually honour: a state it
# dispatches, or one it parks at as a declared gate.
entry_choices() {
  local out="" s
  for s in drafting triage todo; do
    if has_state "$ACTIVE_STATES" "$s" || has_state "$GATE_STATES" "$s"; then
      out="${out:+$out, }$s"
    fi
  done
  printf '%s' "$out"
}

# --- resolve the entry state -------------------------------------------------
if [ "$ENTRY_EXPLICIT" -eq 0 ]; then
  # No --entry. Defaulting to a fixed state is what issue #176 is about, so the
  # default is DERIVED, and refuses loudly where it cannot be: a wrong default
  # here files a ticket nothing will ever pick up, and says nothing.
  if [ -n "$RESOLVE_ERR" ]; then
    cat >&2 <<EOF
ERROR cannot resolve the entry state for $REPO: $RESOLVE_ERR
      The entry state is only meaningful where the project dispatches it, so it
      is not defaulted blind — filing at status:triage against a project that
      never dispatches triage produces a ticket nobody ever picks up.
      Re-run with an explicit --entry {drafting|triage|todo}.
EOF
    exit 2
  elif has_state "$ACTIVE_STATES" "triage"; then
    ENTRY="triage"          # the project verifies tickets before dispatch
  elif has_state "$ACTIVE_STATES" "todo"; then
    ENTRY="todo"            # no triage step here; the dispatchable entry is todo
  else
    cat >&2 <<EOF
ERROR project '$PROJECT_SLUG' dispatches neither 'triage' nor 'todo', so there is
      no entry state new-ticket.sh can default to.
      dispatches: $(list_states "$ACTIVE_STATES")  (from $WORKFLOW_PATH)
      Re-run with an explicit --entry {drafting|triage|todo}.
EOF
    exit 2
  fi
elif [ -z "$RESOLVE_ERR" ] \
  && ! has_state "$ACTIVE_STATES" "$ENTRY" && ! has_state "$GATE_STATES" "$ENTRY"; then
  # An explicitly requested state the project neither dispatches nor gates is
  # the same dead ticket the default path refuses — the explicit case must not
  # be silently worse than the defaulted one.
  cat >&2 <<EOF
ERROR --entry $ENTRY files status:$ENTRY, which project '$PROJECT_SLUG' neither
      dispatches nor declares as a gate — nothing there would move the ticket.
      dispatches: $(list_states "$ACTIVE_STATES")
      gates:      $(list_states "${GATE_STATES:-(none)|}")
      workflow:   $WORKFLOW_PATH
      entry states this project honours: $(entry_choices)
EOF
  exit 2
fi

LABEL="status:$ENTRY"
# A `todo` entry skips triage by design — either because the filer chose it for
# trivial, bounded criteria (see README "Choosing the entry state"), or because
# the project's stance has no triage step at all. Both stamp the provenance
# marker the dispatch guard requires (issue #29 / AgDR-011), because an
# unstamped status:todo is refused and never dispatched — which would re-create
# the never-dispatched ticket this resolution exists to prevent. See AgDR-049.
if [ "$ENTRY" = "todo" ]; then
  LABEL="$LABEL,gate:triage-passed"
fi

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
project:    ${PROJECT_SLUG:-(unresolved)}
workflow:   ${WORKFLOW_PATH:-(unresolved: $RESOLVE_ERR)}
dispatches: $([ -n "$ACTIVE_STATES" ] && list_states "$ACTIVE_STATES" || printf '(unknown)')
entry:      $ENTRY ($([ "$ENTRY_EXPLICIT" -eq 1 ] && printf 'explicit' || printf 'resolved from active_states'))
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
