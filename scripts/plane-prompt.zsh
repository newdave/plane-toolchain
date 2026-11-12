#!/usr/bin/env zsh
# Sourced by the project mise hook; no user-level shell files are changed.
_plane_toolchain_root=${1:?project root required}

_plane_toolchain_prompt() {
  local plane_state
  if [[ -n ${_plane_toolchain_prefix-} && $PS1 == "$_plane_toolchain_prefix"* ]]; then
    PS1=${PS1#"$_plane_toolchain_prefix"}
  fi
  plane_state=$(python3 "$_plane_toolchain_root/src/plane_toolchain/session.py" status 2>/dev/null) || plane_state=invalid
  _plane_toolchain_prefix="[Plane:${plane_state}] "
  PS1="$_plane_toolchain_prefix$PS1"
}

_plane_toolchain_prompt_remove() {
  if [[ -n ${_plane_toolchain_prefix-} && $PS1 == "$_plane_toolchain_prefix"* ]]; then
    PS1=${PS1#"$_plane_toolchain_prefix"}
  fi
  precmd_functions=(${precmd_functions:#_plane_toolchain_prompt})
  unset _plane_toolchain_root _plane_toolchain_prefix
  unfunction _plane_toolchain_prompt _plane_toolchain_prompt_remove
}

typeset -ga precmd_functions
if ((! ${precmd_functions[(Ie)_plane_toolchain_prompt]})); then
  precmd_functions+=(_plane_toolchain_prompt)
fi
