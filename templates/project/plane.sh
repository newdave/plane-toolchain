#!/usr/bin/env bash
# Installed by plane-toolchain scripts/install_project.py; revert it with the same script.
# Usage: .plane/plane.sh mcp|session|status|enable|disable
set +x
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PLANE_CONFIG_ROOT="$root"
# Exported PLANE_TOOLCHAIN wins; otherwise read the literal line from ignored .plane/local.env.
toolchain="${PLANE_TOOLCHAIN:-}"
if [[ -z "$toolchain" && -f "$root/.plane/local.env" ]]; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" == PLANE_TOOLCHAIN=* ]] && toolchain="${line#PLANE_TOOLCHAIN=}"
  done <"$root/.plane/local.env"
fi
session="$toolchain/src/plane_toolchain/session.py"
missing="plane-toolchain not found; set PLANE_TOOLCHAIN in .plane/local.env"
case "${1:-}" in
  session)
    # Startup hooks report problems but never block the client.
    [[ -f "$session" ]] && exec python3 "$session"
    printf '{"systemMessage": "Plane: invalid (%s)."}\n' "$missing"
    ;;
  status)
    [[ -f "$session" ]] || { echo invalid; exit 1; }
    exec python3 "$session" status
    ;;
  mcp)
    [[ -f "$toolchain/scripts/plane-mcp.sh" ]] || { echo "$missing" >&2; exit 1; }
    # mise supplies the toolchain's pinned Node for mcp-remote.
    if command -v mise >/dev/null 2>&1; then
      exec mise -C "$toolchain" exec -- bash "$toolchain/scripts/plane-mcp.sh"
    fi
    exec bash "$toolchain/scripts/plane-mcp.sh"
    ;;
  enable | disable)
    [[ -f "$session" ]] || { echo "$missing" >&2; exit 1; }
    cd "$toolchain"
    exec uv run --locked python -m plane_toolchain.config "$1"
    ;;
  *)
    echo "Usage: .plane/plane.sh mcp|session|status|enable|disable" >&2
    exit 2
    ;;
esac
