#!/usr/bin/env bash
# Project-only stdio bridge. Secrets arrive through the environment or an encrypted file.
set +x
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
PLANE_ENABLED="$(python3 src/plane_toolchain/session.py status)"
export PLANE_ENABLED
case "$PLANE_ENABLED" in
  enabled) export PLANE_ENABLED=true ;;
  disabled) export PLANE_ENABLED=false ;;
  *) exit 1 ;;
esac
# Non-secret values from ignored config/local.env; exported values take precedence.
while IFS= read -r -d '' entry; do
  export "${entry%%=*}=${entry#*=}"
done < <(python3 src/plane_toolchain/session.py connection)
# Optional SOPS-encrypted key file (ignored by *.enc.env). Only PLANE_API_KEY is exported.
secrets=config/plane.enc.env
if [[ -z "${PLANE_API_KEY:-}" && -f "$secrets" ]]; then
  # Clients launch the bridge outside mise; use the same key path on macOS and Linux.
  if [[ -z "${SOPS_AGE_KEY_FILE:-}" && -f "$HOME/.config/sops/age/keys.txt" ]]; then
    export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"
  fi
  sops_cmd=(sops)
  command -v sops >/dev/null 2>&1 || sops_cmd=(mise exec -- sops)
  while IFS= read -r line; do
    [[ "$line" == PLANE_API_KEY=* ]] && export PLANE_API_KEY="${line#PLANE_API_KEY=}"
  done < <("${sops_cmd[@]}" decrypt --output-type dotenv "$secrets")
fi
uv run --locked python -m plane_toolchain.settings
# mcp-remote expands headers from environment, keeping credentials out of argv.
# shellcheck disable=SC2016  # literal ${VAR}: mcp-remote expands it, not the shell
exec npx --yes mcp-remote@0.16.6 "$PLANE_MCP_URL" \
  --transport http-only --silent \
  --header 'Authorization:Bearer ${PLANE_API_KEY}' \
  --header 'x-workspace-slug:${PLANE_WORKSPACE}'
