#!/usr/bin/env bash
# list-projects.sh — enumerate registered project bindings.
set -euo pipefail

SB_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

shopt -s nullglob
FOUND=0
for env in "$SB_HOME"/projects/*/project.env; do
  FOUND=1
  slug="$(basename "$(dirname "$env")")"
  repo="$(sed -n 's/^SB_GITHUB_REPO=//p' "$env")"
  base="$(sed -n 's/^SB_BASE_BRANCH=//p' "$env")"
  ws="$(sed -n 's/^SB_WORKSPACE_ROOT=//p' "$env")"
  printf '%-20s %-40s base=%-8s %s\n' "$slug" "$repo" "$base" "$ws"
done
[ "$FOUND" -eq 1 ] || echo "no projects registered (scripts/register-project.sh --slug ... --repo ...)"
