#!/usr/bin/env bash
# Installed by plane-toolchain scripts/install_project.py; revert it with the same script.
# Usage: .plane/plane.sh mcp|session|status|enable|disable
set +x
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PLANE_CONFIG_ROOT="$root"
# Exported PLANE_TOOLCHAIN wins; otherwise read the literal line from ignored config/local.env
# (or, until it is moved, .plane/local.env, read first so config/local.env overrides it).
toolchain="${PLANE_TOOLCHAIN:-}"
# In a linked worktree the main checkout's file is read first (in place, never copied);
# the worktree's own file overrides it. Symlinks and a non-".git" common dir get no fallback.
files=()
main=""
if dirs="$(git -C "$root" rev-parse --absolute-git-dir --path-format=absolute --git-common-dir 2>/dev/null)"; then
  gitdir="${dirs%%$'\n'*}"
  common="${dirs#*$'\n'}"
  [[ "$gitdir" != "$common" && "$(basename "$common")" == .git ]] && main="$(dirname "$common")"
fi
for rel in .plane/local.env config/local.env; do
  shared="$main/$rel"
  if [[ -n "$main" && -f "$shared" && ! -L "$shared" ]]; then
    real="$(cd -P "$(dirname "$shared")" && pwd)"
    [[ "$real" == "$(cd -P "$main" && pwd)/$(dirname "$rel")" ]] && files+=("$shared")
  fi
  [[ -f "$root/$rel" ]] && files+=("$root/$rel")
done
if [[ -z "$toolchain" ]]; then
  for file in ${files[@]+"${files[@]}"}; do
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" == PLANE_TOOLCHAIN=* ]] && toolchain="${line#PLANE_TOOLCHAIN=}"
    done <"$file"
  done
fi
# Same normalising as the session reader: drop a trailing " # comment", then one pair of quotes.
toolchain="${toolchain%% #*}"
case "$toolchain" in
\"*\") toolchain="${toolchain#\"}" toolchain="${toolchain%\"}" ;;
\'*\') toolchain="${toolchain#\'}" toolchain="${toolchain%\'}" ;;
esac
session="$toolchain/src/plane_toolchain/session.py"
missing="plane-toolchain not found; set PLANE_TOOLCHAIN in config/local.env"
case "${1:-}" in
session)
  # Startup hooks report problems but never block the client.
  [[ -f "$session" ]] && exec python3 "$session"
  printf '{"systemMessage": "Plane: invalid (%s)."}\n' "$missing"
  ;;
status)
  [[ -f "$session" ]] || {
    echo invalid
    exit 1
  }
  exec python3 "$session" status
  ;;
mcp)
  [[ -f "$toolchain/scripts/plane-mcp.sh" ]] || {
    echo "$missing" >&2
    exit 1
  }
  # mise supplies the toolchain's pinned Node for mcp-remote.
  if command -v mise >/dev/null 2>&1; then
    exec mise -C "$toolchain" exec -- bash "$toolchain/scripts/plane-mcp.sh"
  fi
  exec bash "$toolchain/scripts/plane-mcp.sh"
  ;;
enable | disable)
  [[ -f "$session" ]] || {
    echo "$missing" >&2
    exit 1
  }
  cd "$toolchain"
  exec env -u VIRTUAL_ENV uv run --locked python -m plane_toolchain.config "$1"
  ;;
*)
  echo "Usage: .plane/plane.sh mcp|session|status|enable|disable" >&2
  exit 2
  ;;
esac
