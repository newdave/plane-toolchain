"""Ignored bridge files resolve from the main checkout when running in a git worktree."""

import json
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


def lockfile(tag, version="1.0.0"):
    """A package-lock.json; the tag makes lockfiles differ without changing the pin."""
    return json.dumps({"tag": tag, "packages": {"node_modules/mcp-remote": {"version": version}}})


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

    def install(root, lock="lock-a"):
        # The lockfile is tracked in a real checkout, so every worktree has its own.
        write(root, "tools/mcp-remote/package-lock.json", lockfile(lock))
        write(root, "scripts/plane-mcp.sh", (REPO / "scripts/plane-mcp.sh").read_text())
        dest = root / "src/plane_toolchain"
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / "src/plane_toolchain/session.py", dest / "session.py")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    # sops reports the file it was asked to decrypt; the key is a fixture value.
    write(
        stubs,
        "sops",
        '#!/bin/sh\necho "SOPS_FILE=$4" >&2\n'
        '[ -z "$SOPS_FAIL" ] || { echo "sops: no key could decrypt" >&2; exit 1; }\n'
        "echo PLANE_API_KEY=fixture\n",
    )
    write(stubs, "uv", "#!/bin/sh\nexit 0\n")
    for name in ("sops", "uv"):
        (stubs / name).chmod(0o755)

    def run(root, **extra):
        env = {
            "PATH": f"{stubs}:{os.environ['PATH']}",
            "HOME": str(tmp_path / "home"),
            "PLANE_ENABLED": "true",
            "PLANE_MCP_URL": "https://mcp.example.org/mcp",
            "PLANE_WORKSPACE": "demo",
            **extra,
        }
        return subprocess.run(  # noqa: S603 - fixed argv
            ["bash", str(root / "scripts/plane-mcp.sh")],  # noqa: S607
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )

    def stub_mcp_remote(root, tag, lock="lock-a", version="1.0.0"):
        write(root, "tools/mcp-remote/package-lock.json", lockfile(lock))
        write(
            root,
            "tools/mcp-remote/node_modules/mcp-remote/package.json",
            json.dumps({"name": "mcp-remote", "version": version}),
        )
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


def test_script_worktree_lockfile_mismatch_fails_closed(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    stub(main, "main-bin")
    wt = worktree(main)
    install(wt, lock="lock-b")
    result = run(wt)
    assert result.returncode == 1
    assert "mise run setup" in result.stderr
    assert "main-bin" not in result.stdout


def test_script_worktree_symlinked_main_files_get_no_fallback(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    stub(main, "main-bin")
    real = tmp_path / "elsewhere"
    real.mkdir()
    write(real, "key", "x")
    (main / "config").mkdir()
    (main / "config/plane.enc.env").symlink_to(real / "key")
    # A bin that links outside main's node_modules is refused even with a matching lockfile.
    bin_path = main / "tools/mcp-remote/node_modules/.bin/mcp-remote"
    write(real, "evil", "#!/bin/sh\necho evil\n")
    (real / "evil").chmod(0o755)
    bin_path.unlink()
    bin_path.symlink_to(real / "evil")
    wt = worktree(main)
    install(wt)
    result = run(wt)
    assert result.returncode == 1
    assert "evil" not in result.stdout
    assert "SOPS_FILE" not in result.stderr


def test_script_worktree_accepts_symlinked_bin_inside_main(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    stub(main, "main-bin")
    bin_dir = main / "tools/mcp-remote/node_modules/.bin"
    target = main / "tools/mcp-remote/node_modules/pkg/cli.sh"
    write(main, "tools/mcp-remote/node_modules/pkg/cli.sh", "#!/bin/sh\necho linked\n")
    target.chmod(0o755)
    (bin_dir / "mcp-remote").unlink()
    (bin_dir / "mcp-remote").symlink_to("../pkg/cli.sh")
    wt = worktree(main)
    install(wt)
    result = run(wt)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "linked"


def test_session_symlinked_main_file_and_odd_common_dir_get_no_fallback(tmp_path):
    main = make_repo(tmp_path / "main")
    other = tmp_path / "other.env"
    other.write_text("PLANE_WORKSPACE=linked\n")
    (main / "config").mkdir()
    (main / "config/local.env").symlink_to(other)
    assert plane_session.connection(worktree(main), {}) == {}
    # A common dir not named .git (a bare repository) gives no main checkout.
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(main), str(bare))
    git(bare, "worktree", "add", "-q", str(tmp_path / "bwt"), "main")
    assert plane_session.main_checkout(tmp_path / "bwt") is None


def test_script_installed_worktree_reads_key_file_in_place(tmp_path, bridge):
    install, run, stub = bridge
    toolchain = make_repo(tmp_path / "toolchain")
    install(toolchain)
    stub(toolchain, "tool-bin")
    project = make_repo(tmp_path / "proj")
    write(project, ".plane/plane.enc.env", "fixture-key-file")
    wt = worktree(project)
    result = run(toolchain, PLANE_CONFIG_ROOT=str(wt))
    assert result.returncode == 0, result.stderr
    assert "main checkout" in result.stderr
    used = [x for x in result.stderr.splitlines() if x.startswith("SOPS_FILE=")][0]
    assert (
        Path(used.removeprefix("SOPS_FILE=")).resolve()
        == (project / ".plane/plane.enc.env").resolve()
    )
    assert not (wt / ".plane/plane.enc.env").exists()
    assert "fixture" not in result.stdout + result.stderr.replace("fixture-key-file", "")
    # A symlinked key file in the main checkout is not followed.
    (project / ".plane/plane.enc.env").unlink()
    (project / ".plane/plane.enc.env").symlink_to(tmp_path / "stubs/sops")
    assert "SOPS_FILE" not in run(toolchain, PLANE_CONFIG_ROOT=str(wt)).stderr


def test_script_reports_a_sops_failure_instead_of_a_missing_key(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    install(main)
    write(main, "config/plane.enc.env", "x")
    stub(main, "main-bin")
    result = run(main, SOPS_FAIL="1")
    assert result.returncode == 1
    assert "could not decrypt config/plane.enc.env" in result.stderr
    assert "SOPS_AGE_KEY_FILE" in result.stderr
    assert "main-bin" not in result.stdout


def test_script_refuses_a_symlinked_secrets_file(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    install(main)
    stub(main, "main-bin")
    real = tmp_path / "elsewhere"
    real.mkdir()
    write(real, "key", "x")
    (main / "config").mkdir()
    (main / "config/plane.enc.env").symlink_to(real / "key")
    result = run(main)
    assert result.returncode == 1
    assert "symlink" in result.stderr
    assert "SOPS_FILE" not in result.stderr
    assert "main-bin" not in result.stdout


def test_script_ignores_a_symlinked_secrets_file_when_the_key_is_exported(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    install(main)
    stub(main, "main-bin")
    (main / "config").mkdir()
    (main / "config/plane.enc.env").symlink_to(tmp_path / "nowhere")
    result = run(main, PLANE_API_KEY="exported")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "main-bin"


def test_script_refuses_a_symlinked_project_secrets_file(tmp_path, bridge):
    install, run, stub = bridge
    toolchain = make_repo(tmp_path / "toolchain")
    install(toolchain)
    stub(toolchain, "tool-bin")
    project = make_repo(tmp_path / "proj")
    write(tmp_path, "key", "x")
    (project / ".plane").mkdir()
    (project / ".plane/plane.enc.env").symlink_to(tmp_path / "key")
    result = run(toolchain, PLANE_CONFIG_ROOT=str(project))
    assert result.returncode == 1
    assert "symlink" in result.stderr
    assert "SOPS_FILE" not in result.stderr


def test_script_worktree_refuses_main_node_modules_that_differ_from_the_lock(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    stub(main, "main-bin", version="0.0.1")  # lock pins 1.0.0
    wt = worktree(main)
    install(wt)
    result = run(wt)
    assert result.returncode == 1
    assert "does not match the lockfile" in result.stderr
    assert "mise run setup" in result.stderr
    assert "main-bin" not in result.stdout


def test_script_main_checkout_refuses_node_modules_that_differ_from_the_lock(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    install(main)
    stub(main, "main-bin", version="0.0.1")
    result = run(main)
    assert result.returncode == 1
    assert "does not match the lockfile" in result.stderr


def test_script_worktree_accepts_main_node_modules_that_match_the_lock(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    stub(main, "main-bin", version="1.0.0")
    wt = worktree(main)
    install(wt)
    assert run(wt).stdout.strip() == "main-bin"


def test_script_says_so_when_git_is_too_old_for_the_worktree_fallback(tmp_path, bridge):
    install, run, stub = bridge
    main = make_repo(tmp_path / "main")
    install(main)
    stub(main, "main-bin")
    real_git = shutil.which("git")
    old = tmp_path / "old-git"
    old.mkdir()
    write(
        old,
        "git",
        f'#!/bin/sh\ncase "$*" in *--path-format*) echo "usage: git rev-parse" >&2; exit 129;; esac\n'
        f'exec "{real_git}" "$@"\n',
    )
    (old / "git").chmod(0o755)
    result = run(main, PATH=f"{old}:{tmp_path / 'stubs'}:{os.environ['PATH']}")
    assert "git 2.31" in result.stderr
    assert result.stdout.strip() == "main-bin"
    # A current git says nothing.
    assert "git 2.31" not in run(main).stderr


def test_script_outside_git_stays_quiet_about_git_versions(tmp_path, bridge):
    install, run, stub = bridge
    plain = tmp_path / "plain"
    plain.mkdir()
    install(plain)
    stub(plain, "plain-bin")
    assert "git 2.31" not in run(plain).stderr
