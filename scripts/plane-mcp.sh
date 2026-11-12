#!/usr/bin/env bash
# Project-only stdio bridge. Secrets arrive through the environment or an encrypted file.
set +x
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
export PLANE_ENABLED="$(python3 src/plane_toolchain/session.py status)"
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
  sops_cmd=(sops)
  command -v sops >/dev/null 2>&1 || sops_cmd=(mise exec -- sops)
  while IFS= read -r line; do
    [[ "$line" == PLANE_API_KEY=* ]] && export PLANE_API_KEY="${line#PLANE_API_KEY=}"
  done < <("${sops_cmd[@]}" decrypt --output-type dotenv "$secrets")
fi
uv run --locked python -m plane_toolchain.settings
# mcp-remote expands headers from environment, keeping credentials out of argv.
exec npx --yes mcp-remote@0.16.6 "$PLANE_MCP_URL" \
  --transport http-only --silent \
  --header 'Authorization:Bearer ${PLANE_API_KEY}' \
  --header 'x-workspace-slug:${PLANE_WORKSPACE}'
