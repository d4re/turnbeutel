#!/bin/sh
# Call the Turnbeutel API with the planner token.
#
#   api.sh METHOD PATH [extra curl args]
#   api.sh GET /api/planner/profile
#   api.sh POST /api/planner/runs -d @run.json
#
# TURNBEUTEL_URL and PLANNER_TOKEN come from the environment, or else from
# .env in the repo root (from a worktree: the main checkout's root). Prints
# the response body, then "HTTP <status>" on stderr.
set -eu

load_env() {
  [ -f "$1" ] || return 1
  set -a
  # shellcheck disable=SC1090
  . "$1"
  set +a
}

if [ -z "${TURNBEUTEL_URL:-}" ] || [ -z "${PLANNER_TOKEN:-}" ]; then
  top=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
  common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)
  load_env "$top/.env" || { [ -n "$common" ] && load_env "$(dirname "$common")/.env"; } || true
fi

if [ -z "${TURNBEUTEL_URL:-}" ] || [ -z "${PLANNER_TOKEN:-}" ]; then
  echo "api.sh: TURNBEUTEL_URL and PLANNER_TOKEN must be set (environment or .env in the repo root)" >&2
  exit 2
fi

method=$1
path=$2
shift 2
curl -sS -X "$method" -H "Authorization: Bearer $PLANNER_TOKEN" -H "Content-Type: application/json" \
  --write-out '\n%{stderr}HTTP %{http_code}\n' "${TURNBEUTEL_URL%/}$path" "$@"
