#!/usr/bin/env bash
# Project-only stdio bridge. Secrets arrive through the environment or an encrypted file.
set +x
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# Linked worktrees lack git-ignored files; fall back to the main checkout's copy.
# No fallback without git, in a main checkout, or in a copy nested in an unrelated repo.
main=""
if dirs="$(git rev-parse --absolute-git-dir --path-format=absolute --git-common-dir 2>/dev/null)"; then
  gitdir="${dirs%%$'\n'*}"
  common="${dirs#*$'\n'}"
  [[ "$gitdir" == "$common" || "$(basename "$common")" != .git ]] || main="$(dirname "$common")"
elif git rev-parse --git-dir >/dev/null 2>&1; then
  # Inside a repository the command only fails on a git without --path-format (before 2.31).
  echo "plane: git 2.31 or newer is needed to use a main checkout's files from a worktree; none used" >&2
fi
# Succeed only when the real path of $1 is inside directory $2 (symlinks resolved).
inside() {
  python3 -c 'import os,sys
p, d = os.path.realpath(sys.argv[1]), os.path.realpath(sys.argv[2])
sys.exit(0 if p.startswith(d + os.sep) else 1)' "$1" "$2"
}
# Print the local path if present, else the main checkout's file, else the local path.
# The main file must stay inside the main checkout and, unless $2 is "link", not be a symlink.
pick() {
  if [[ ! -e "$1" && -n "$main" && -f "$main/$1" && ( "${2:-}" == link || ! -L "$main/$1" ) ]] &&
    inside "$main/$1" "$main"; then
    echo "$main/$1"
  else
    echo "$1"
  fi
}
digest() { python3 -c 'import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$1"; }
PLANE_ENABLED="$(python3 src/plane_toolchain/session.py status)"
export PLANE_ENABLED
case "$PLANE_ENABLED" in
  enabled) export PLANE_ENABLED=true ;;
  disabled) export PLANE_ENABLED=false ;;
  *) exit 1 ;;
esac
# Non-secret values from ignored config/local.env; exported values take precedence.
# Capture first: a process substitution would hide the exit status (pipefail is set). Values
# are single-line, so NUL separators become newlines, as bash strings cannot hold NUL.
if ! entries="$(python3 src/plane_toolchain/session.py connection | tr '\0' '\n')"; then
  echo "plane: could not read the connection settings" >&2
  exit 1
fi
while IFS= read -r entry; do
  [[ -z "$entry" ]] || export "${entry%%=*}=${entry#*=}"
done < <(printf '%s\n' "$entries")
# Optional SOPS-encrypted key file (ignored by *.enc.env). Only PLANE_API_KEY is exported.
# Installed projects (PLANE_CONFIG_ROOT, set by .plane/plane.sh) keep theirs under .plane/.
secrets="$(pick config/plane.enc.env)"
if [[ -n "${PLANE_CONFIG_ROOT:-}" ]]; then
  secrets="$PLANE_CONFIG_ROOT/.plane/plane.enc.env"
  # An installed project used from its own linked worktree falls back to its main checkout.
  if [[ ! -e "$secrets" ]] && pmain="$(python3 src/plane_toolchain/session.py main "$PLANE_CONFIG_ROOT")"; then
    alt="$pmain/.plane/plane.enc.env"
    if [[ -f "$alt" && ! -L "$alt" ]] && inside "$alt" "$pmain"; then
      secrets="$alt"
      echo "plane: using the main checkout's .plane/plane.enc.env ($pmain)" >&2
    fi
  fi
fi
if [[ -z "${PLANE_API_KEY:-}" && -L "$secrets" ]]; then
  echo "plane: refusing symlinked secrets file $secrets" >&2
  exit 1
fi
if [[ -z "${PLANE_API_KEY:-}" && -f "$secrets" ]]; then
  # Clients launch the bridge outside mise; use the same key path on macOS and Linux.
  if [[ -z "${SOPS_AGE_KEY_FILE:-}" && -f "$HOME/.config/sops/age/keys.txt" ]]; then
    export SOPS_AGE_KEY_FILE="$HOME/.config/sops/age/keys.txt"
  fi
  sops_cmd=(sops)
  command -v sops >/dev/null 2>&1 || sops_cmd=(mise exec -- sops)
  # Capture first: a process substitution would hide sops' exit status.
  if ! decrypted="$("${sops_cmd[@]}" decrypt --output-type dotenv "$secrets")"; then
    echo "plane: could not decrypt $secrets with sops; check that sops is installed and SOPS_AGE_KEY_FILE points at your age key" >&2
    exit 1
  fi
  # Never feed this with a here-string: bash before 5.1 (macOS /bin/bash 3.2) backs it with a
  # temporary file, which would put the decrypted key on disk. printf is a builtin and the
  # process substitution is a pipe, so nothing is written.
  while IFS= read -r line; do
    [[ "$line" == PLANE_API_KEY=* ]] && export PLANE_API_KEY="${line#PLANE_API_KEY=}"
  done < <(printf '%s\n' "$decrypted")
  unset decrypted
fi
env -u VIRTUAL_ENV uv run --locked python -m plane_toolchain.settings
# Succeed only when the mcp-remote installed under node_modules dir $2 has the version that
# lockfile $1 pins; npm ci keeps them equal, a stale or hand-edited tree does not.
locked() {
  python3 -c 'import json,sys
want = json.load(open(sys.argv[1]))["packages"]["node_modules/mcp-remote"]["version"]
have = json.load(open(sys.argv[2] + "/mcp-remote/package.json"))["version"]
sys.exit(0 if want == have else 1)' "$1" "$2" 2>/dev/null
}
# Locked tree from tools/mcp-remote (npm ci); never fetched or resolved at runtime.
bin="$(pick tools/mcp-remote/node_modules/.bin/mcp-remote link)"
# A reused main-checkout tree must match this checkout's lockfile, else it never runs.
if [[ "$bin" != tools/* ]]; then
  lock=tools/mcp-remote/package-lock.json
  if [[ ! -f "$lock" || ! -f "$main/$lock" || "$(digest "$lock")" != "$(digest "$main/$lock")" ]] ||
    ! inside "$bin" "$main/tools/mcp-remote/node_modules"; then
    echo "mcp-remote missing or lockfile differs from main checkout: run 'mise run setup'" >&2
    exit 1
  fi
fi
[[ -x "$bin" ]] || { echo "mcp-remote missing: run 'mise run setup'" >&2; exit 1; }
if ! locked tools/mcp-remote/package-lock.json "$(dirname "$(dirname "$bin")")"; then
  echo "mcp-remote in $(dirname "$(dirname "$bin")") does not match the lockfile: run 'mise run setup'" >&2
  exit 1
fi
# mcp-remote expands headers from environment, keeping credentials out of argv.
# shellcheck disable=SC2016  # literal ${VAR}: mcp-remote expands it, not the shell
exec "$bin" "$PLANE_MCP_URL" \
  --transport http-only --silent \
  --header 'Authorization:Bearer ${PLANE_API_KEY}' \
  --header 'x-workspace-slug:${PLANE_WORKSPACE}'
