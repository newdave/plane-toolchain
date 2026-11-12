#!/usr/bin/env bash
# Project-only stdio bridge. Secrets arrive through the environment.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PLANE_ENABLED="$(python3 src/plane_toolchain/session.py status)"
case "$PLANE_ENABLED" in
  enabled) export PLANE_ENABLED=true ;;
  disabled) export PLANE_ENABLED=false ;;
  *) exit 1 ;;
esac
uv run --locked python -m plane_toolchain.settings
# mcp-remote expands headers from environment, keeping credentials out of argv.
exec npx --yes mcp-remote@0.16.6 "$PLANE_MCP_URL" \
  --transport http-only --silent \
  --header 'Authorization:Bearer ${PLANE_API_KEY}' \
  --header 'x-workspace-slug:${PLANE_WORKSPACE}'
