"""plane-prompt.zsh: the cached status expires after 30 seconds without any input change."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def test_zsh_prompt_cache_expires_after_thirty_seconds(tmp_path):
    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("zsh unavailable")
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "src/plane_toolchain").mkdir(parents=True)
    shutil.copyfile(
        root / "src/plane_toolchain/session.py", tmp_path / "src/plane_toolchain/session.py"
    )
    (tmp_path / "config").mkdir()
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    real = shutil.which("python3")
    (stubs / "python3").write_text(f'#!/bin/sh\necho x >> "$COUNT"\nexec "{real}" "$@"\n')
    (stubs / "python3").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "PLANE_ENABLED"}
    env.update(
        COUNT=str(tmp_path / "count"),
        PATH=f"{stubs}:{os.environ['PATH']}",
        MISE_PROJECT_ROOT=str(tmp_path),
        PROMPT_SCRIPT=str(root / "scripts/plane-prompt.zsh"),
    )
    result = subprocess.run(  # noqa: S603 - fixed local shell fixture
        [
            zsh,
            "-f",
            "-c",
            """
zmodload zsh/datetime || exit 9
PS1='original> '
source "$PROMPT_SCRIPT" "$MISE_PROJECT_ROOT"
_plane_toolchain_prompt
_plane_toolchain_prompt
[[ $(wc -l < "$COUNT") -eq 1 ]] || exit 1
_plane_toolchain_at=$(( EPOCHSECONDS - 29 ))
_plane_toolchain_prompt
[[ $(wc -l < "$COUNT") -eq 1 ]] || exit 2
_plane_toolchain_at=$(( EPOCHSECONDS - 31 ))
_plane_toolchain_prompt
[[ $(wc -l < "$COUNT") -eq 2 ]] || exit 3
[[ $PS1 == '[Plane:disabled] original> ' ]] || exit 4
""",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (result.returncode, result.stderr)
