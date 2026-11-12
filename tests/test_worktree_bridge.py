"""Ignored bridge files resolve from the main checkout when running in a git worktree."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from plane_toolchain import session as plane_session

REPO = Path(__file__).resolve().parents[1]
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.org"]


def git(cwd, *args):
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)  # noqa: S603 - fixed argv


def make_repo(path):
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "commit", "-q", "--allow-empty", "-m", "init")
    return path


def worktree(main, name="wt"):
    path = main.parent / name
    git(main, "worktree", "add", "-q", str(path))
    return path


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_session_main_checkout_uses_own_file(tmp_path):
    main = make_repo(tmp_path / "main")
    write(main, "config/local.env", "PLANE_WORKSPACE=own\n")
    assert plane_session.connection(main, {}) == {"PLANE_WORKSPACE": "own"}


def test_session_worktree_falls_back_to_main_file(tmp_path):
    main = make_repo(tmp_path / "main")
    write(main, "config/local.env", "PLANE_WORKSPACE=shared\n")
    assert plane_session.connection(worktree(main), {}) == {"PLANE_WORKSPACE": "shared"}


def test_session_worktree_own_file_wins(tmp_path):
    main = make_repo(tmp_path / "main")
    write(main, "config/local.env", "PLANE_WORKSPACE=shared\n")
    wt = worktree(main)
    write(wt, "config/local.env", "PLANE_WORKSPACE=mine\n")
    assert plane_session.connection(wt, {}) == {"PLANE_WORKSPACE": "mine"}


def test_session_no_fallback_outside_git(tmp_path):
    write(tmp_path, "config/local.env", "PLANE_WORKSPACE=x\n")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert plane_session.connection(plain, {}) == {}


@pytest.fixture
def bridge(tmp_path):
    """Run the real script from a copy inside a temp checkout, with stub sops, uv and mcp-remote."""

    def install(root):
        write(root, "scripts/plane-mcp.sh", (REPO / "scripts/plane-mcp.sh").read_text())
        dest = root / "src/plane_toolchain"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "src/plane_toolchain/session.py", dest / "session.py")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    # sops reports the file it was asked to decrypt; the key is a fixture value.
    write(stubs, "sops", '#!/bin/sh\necho "SOPS_FILE=$4" >&2\necho PLANE_API_KEY=fixture\n')
    write(stubs, "uv", "#!/bin/sh\nexit 0\n")
    for name in ("sops", "uv"):
        (stubs / name).chmod(0o755)

    def run(root):
        env = {
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "HOME": str(tmp_path / "home"),
            "PLANE_ENABLED": "true",
            "PLANE_MCP_URL": "https://mcp.example.org/mcp",
            "PLANE_WORKSPACE": "demo",
        }
        return subprocess.run(  # noqa: S603 - fixed argv
            ["bash", str(root / "scripts/plane-mcp.sh")],  # noqa: S607
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )

    def stub_mcp_remote(root, tag):
        path = root / "tools/mcp-remote/node_modules/.bin/mcp-remote"
        write(root, "tools/mcp-remote/node_modules/.bin/mcp-remote", f'#!/bin/sh\necho "{tag}"\n')
        path.chmod(0o755)

    return install, run, stub_mcp_remote


def test_script_main_checkout_uses_own_items(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    install(main)
    write(main, "config/plane.enc.env", "x")
    stub(main, "main-bin")
    result = run(main)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "main-bin"
    assert result.stderr.strip() == "SOPS_FILE=config/plane.enc.env"


def test_script_worktree_falls_back_to_main_items(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    write(main, "config/plane.enc.env", "x")
    stub(main, "main-bin")
    wt = worktree(main)
    install(wt)
    result = run(wt)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "main-bin"
    used = Path(result.stderr.strip().removeprefix("SOPS_FILE="))
    assert used.resolve() == (main / "config/plane.enc.env").resolve()


def test_script_worktree_own_items_win(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    write(main, "config/plane.enc.env", "x")
    stub(main, "main-bin")
    wt = worktree(main)
    install(wt)
    write(wt, "config/plane.enc.env", "y")
    stub(wt, "wt-bin")
    result = run(wt)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "wt-bin"
    assert result.stderr.strip() == "SOPS_FILE=config/plane.enc.env"


def test_script_no_fallback_outside_git(tmp_path, bridge):
    install, run, _ = bridge
    plain = tmp_path / "plain"
    plain.mkdir()
    install(plain)
    result = run(plain)
    assert result.returncode == 1
    assert "mcp-remote missing" in result.stderr


def test_session_merges_main_and_worktree_keys(tmp_path):
    main = make_repo(tmp_path / "main")
    write(
        main,
        "config/local.env",
        "PLANE_WORKSPACE=shared\nPLANE_MCP_URL=https://mcp.example.org/m\n",
    )
    wt = worktree(main)
    write(wt, "config/local.env", "PLANE_WORKSPACE=mine\n")
    assert plane_session.connection(wt, {}) == {
        "PLANE_WORKSPACE": "mine",
        "PLANE_MCP_URL": "https://mcp.example.org/m",
    }


def test_session_worktree_legacy_file_ignores_main_local(tmp_path):
    main = make_repo(tmp_path / "main")
    write(main, "config/local.env", "PLANE_MCP_URL=https://mcp.example.org/m\n")
    wt = worktree(main)
    write(wt, ".deploy/env", "PLANE_WORKSPACE=legacy\n")
    assert plane_session.connection(wt, {}) == {"PLANE_WORKSPACE": "legacy"}


def test_session_nested_copy_in_unrelated_repo_has_no_fallback(tmp_path):
    outer = make_repo(tmp_path / "outer")
    write(outer, "config/local.env", "PLANE_WORKSPACE=outer\n")
    nested = outer / "copy"
    nested.mkdir()
    assert plane_session.connection(nested, {}) == {}


def test_script_nested_copy_in_unrelated_repo_has_no_fallback(tmp_path, bridge):
    install, run, stub = bridge
    outer = make_repo(tmp_path / "outer")
    write(outer, "config/plane.enc.env", "x")
    stub(outer, "outer-bin")
    nested = outer / "copy"
    install(nested)
    result = run(nested)
    assert result.returncode == 1
    assert "SOPS_FILE" not in result.stderr
    assert "outer-bin" not in result.stdout
    assert "mcp-remote missing" in result.stderr
