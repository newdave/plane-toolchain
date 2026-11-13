#!/usr/bin/env zsh
# Sourced by the project mise hook; no user-level shell files are changed.
_plane_toolchain_root=${1:?project root required}

# The status is cached: python3 runs again only when a config file or PLANE_ENABLED changed,
# or after 30 seconds (the main checkout's files in a worktree are not in the key). Without
# the zsh modules below there is no cache, and every prompt runs python3 as before.
if zmodload -F zsh/stat b:zstat 2>/dev/null && zmodload -F zsh/datetime p:EPOCHSECONDS 2>/dev/null; then
  _plane_toolchain_cached=1
else
  _plane_toolchain_cached=0
fi

_plane_toolchain_key() {
  local file
  local -a mtime size
  _plane_toolchain_stamp="${PLANE_ENABLED-unset}"
  for file in config/defaults.env .env config/local.env .deploy/env .plane/local.env; do
    mtime=(absent) size=()
    if [[ -e $_plane_toolchain_root/$file ]]; then
      zstat -A mtime +mtime -- "$_plane_toolchain_root/$file" 2>/dev/null || mtime=(unreadable)
      zstat -A size +size -- "$_plane_toolchain_root/$file" 2>/dev/null
    fi
    _plane_toolchain_stamp+="|$file:${mtime[1]}:${size[1]}"
  done
}

_plane_toolchain_prompt() {
  local plane_state
  if [[ -n ${_plane_toolchain_prefix-} && $PS1 == "$_plane_toolchain_prefix"* ]]; then
    PS1=${PS1#"$_plane_toolchain_prefix"}
  fi
  plane_state=
  if ((_plane_toolchain_cached)); then
    _plane_toolchain_key
    if [[ $_plane_toolchain_stamp == "${_plane_toolchain_seen-}" ]] &&
      ((EPOCHSECONDS - ${_plane_toolchain_at:-0} < 30)); then
      plane_state=${_plane_toolchain_state-}
    fi
  fi
  if [[ -z $plane_state ]]; then
    plane_state=$(python3 "$_plane_toolchain_root/src/plane_toolchain/session.py" status 2>/dev/null) || plane_state=invalid
    if ((_plane_toolchain_cached)); then
      _plane_toolchain_seen=$_plane_toolchain_stamp
      _plane_toolchain_state=$plane_state
      _plane_toolchain_at=$EPOCHSECONDS
    fi
  fi
  _plane_toolchain_prefix="[Plane:${plane_state}] "
  PS1="$_plane_toolchain_prefix$PS1"
}

_plane_toolchain_prompt_remove() {
  if [[ -n ${_plane_toolchain_prefix-} && $PS1 == "$_plane_toolchain_prefix"* ]]; then
    PS1=${PS1#"$_plane_toolchain_prefix"}
  fi
  precmd_functions=(${precmd_functions:#_plane_toolchain_prompt})
  unset _plane_toolchain_root _plane_toolchain_prefix _plane_toolchain_cached \
    _plane_toolchain_stamp _plane_toolchain_seen _plane_toolchain_state _plane_toolchain_at
  unfunction _plane_toolchain_prompt _plane_toolchain_prompt_remove _plane_toolchain_key
}

typeset -ga precmd_functions
if ((! ${precmd_functions[(Ie)_plane_toolchain_prompt]})); then
  precmd_functions+=(_plane_toolchain_prompt)
fi
