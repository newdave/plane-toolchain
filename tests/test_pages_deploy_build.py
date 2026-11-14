"""Pages deploy: derived image build."""

import inspect
import json
import subprocess
import sys
import pytest
from plane_toolchain import pages_deploy
from support_pages_deploy import (
    BUILD_NOISE,
    DIGEST,
    IMAGE,
    MUTATIONS,
    SOURCE,
    UPSTREAM,
    UPSTREAM_DIGEST,
    assert_original_restored,
    build,
    deploy,
)
from support_pages_deploy import host  # noqa: F401
from support_pages_deploy import saltbox  # noqa: F401


def test_build_derives_from_upstream_repo_digest_and_prints_tag_and_digest(
    saltbox, monkeypatch, capsys
):
    build(saltbox, monkeypatch)
    assert capsys.readouterr().out == IMAGE + " " + DIGEST + "\n"
    assert saltbox.calls == [
        ("inspect", "plane-worker"),
        ("image", "inspect", UPSTREAM),
        ("build", "-t", IMAGE, "-"),
    ]
    [(tag, files)] = saltbox.builds
    assert tag == IMAGE and files["plane_pages.py"] == SOURCE.encode()
    assert files["Dockerfile"].decode().startswith("FROM " + UPSTREAM_DIGEST + "\n")
    assert saltbox.streams[("build", "-t", IMAGE, "-")] == (subprocess.DEVNULL, subprocess.PIPE)


def test_build_never_mutates_containers(saltbox, monkeypatch):
    before = json.dumps(saltbox.containers, sort_keys=True)
    build(saltbox, monkeypatch)
    assert not {call[0] for call in saltbox.calls} & MUTATIONS
    assert json.dumps(saltbox.containers, sort_keys=True) == before


def test_build_backend_image_override_skips_worker(saltbox, monkeypatch, capsys):
    saltbox.images["custom/backend:9"] = ["custom/backend@sha256:c"]
    build(saltbox, monkeypatch, "custom/backend:9")
    assert saltbox.calls[0] == ("image", "inspect", "custom/backend:9")
    assert saltbox.builds[0][1]["Dockerfile"].decode().startswith("FROM custom/backend@sha256:c\n")
    assert capsys.readouterr().out == IMAGE + " " + DIGEST + "\n"


@pytest.mark.parametrize("digests", [None, []])
def test_build_refuses_upstream_image_missing_or_without_digest(saltbox, monkeypatch, digests):
    if digests is None:
        del saltbox.images[UPSTREAM]
    else:
        saltbox.images[UPSTREAM] = digests
    with pytest.raises(ValueError, match="Upstream image missing or has no registry digest"):
        build(saltbox, monkeypatch)
    assert saltbox.builds == []
    assert not {call[0] for call in saltbox.calls} & MUTATIONS


def test_build_derives_tag_with_registry_port_worker_image(saltbox, monkeypatch):
    saltbox.containers["plane-worker"]["image"] = "registry:5000/x/plane-backend:v2"
    saltbox.images["makeplane/plane-backend:v2"] = [UPSTREAM_DIGEST]
    build(saltbox, monkeypatch)
    assert saltbox.calls[1] == ("image", "inspect", "makeplane/plane-backend:v2")


def test_build_remote_ships_needed_constants():
    assert "PAGES_IMAGE_PREFIX = " in pages_deploy.REMOTE
    assert "PAGES_LABEL = " in pages_deploy.REMOTE
    compile(pages_deploy.REMOTE, "<remote>", "exec")


def test_main_build_needs_only_ssh_and_ships_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["pages_deploy", "build"])
    monkeypatch.setenv("PLANE_MCP_SSH", "owner@host")
    for name in ("PLANE_API_CONTAINER", "PLANE_LIVE_URL", "PLANE_BACKEND_IMAGE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("PLANE_WORKER_CONTAINER", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kw: calls.append((args, kw)))
    pages_deploy.main()
    [(_, kw)] = calls
    sent = json.loads(kw["input"])
    assert sent["operation"] == "build" and sent["worker"] == "plane-worker"
    assert sent["backend_image"] is None and sent["digest"] == sent["digest"].lower()


def test_main_build_rejects_unsafe_worker(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["pages_deploy", "build"])
    monkeypatch.setenv("PLANE_MCP_SSH", "owner@host")
    monkeypatch.setenv("PLANE_WORKER_CONTAINER", "w;rm")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("Must not connect"))
    with pytest.raises(ValueError, match="Invalid deployment destination"):
        pages_deploy.main()


def test_build_failure_reports_a_bounded_stderr_tail_without_config(saltbox, monkeypatch):
    saltbox.error_output = BUILD_NOISE.encode()
    saltbox.fail.append("build")
    with pytest.raises(ValueError, match="Image build failed") as error:
        build(saltbox, monkeypatch)
    message = str(error.value)
    assert "ERROR: boom" in message and "step 39" in message
    assert "step 5" not in message and len(message) < 1200
    assert "\n" not in message


def test_derive_failure_in_apply_uses_the_same_build_helper_and_leaves_api_untouched(host):
    host.error_output = b"ERROR: boom\n"
    host.fail.append("build")
    with pytest.raises(ValueError, match="Image build failed.*ERROR: boom"):
        deploy("apply", host)
    assert_original_restored(host)
    assert not {call[0] for call in host.calls} & {"stop", "rename", "create"}


def test_build_streams_stdout_to_devnull_and_captures_stderr(host):
    deploy("apply", host)
    assert host.streams[("build", "-t", IMAGE, "-")] == (subprocess.DEVNULL, subprocess.PIPE)


def test_build_image_is_a_single_shipped_helper():
    source = inspect.getsource(pages_deploy.derive) + inspect.getsource(pages_deploy.build)
    assert source.count('"build"') == 0
    assert pages_deploy.build_image.__name__ in pages_deploy.REMOTE


def test_build_failure_hides_the_base_image_registry(host):
    host.error_output = (
        b"ERROR: failed to pull registry.example/plane-backend@sha256:abc\n"
        b"manifest for registry.example/plane-backend unknown\n"
    )
    host.fail.append("build")
    host.images["sha256:base"] = ["registry.example/plane-backend@sha256:abc"]
    with pytest.raises(ValueError, match="Image build failed") as error:
        deploy("apply", host)
    assert "registry.example" not in str(error.value) and "<base image>" in str(error.value)


def test_build_failure_hides_the_repository_path_and_the_dns_host(host):
    host.error_output = (
        b"ERROR: pull access denied for plane-backend, repository does not exist\n"
        b"dial tcp: lookup registry-1.example.net on 10.0.0.2:53: no such host\n"
    )
    host.fail.append("build")
    host.images["sha256:base"] = ["registry.example/plane-backend@sha256:abc"]
    with pytest.raises(ValueError, match="Image build failed") as error:
        deploy("apply", host)
    message = str(error.value)
    assert "plane-backend" not in message and "registry-1.example.net" not in message
    assert "<base image>" in message and "lookup <host>" in message


def test_digest_covers_source_replacement_and_dockerfile(monkeypatch):
    base = pages_deploy.shipped_digest(SOURCE)
    assert base == pages_deploy.shipped_digest(SOURCE)
    assert pages_deploy.shipped_digest(SOURCE + "# x\n") != base
    real = pages_deploy.replacement

    def changed(original, image, live_url, digest):
        return real(original, image, live_url, digest)

    monkeypatch.setattr(pages_deploy, "replacement", changed)
    assert pages_deploy.shipped_digest(SOURCE) != base  # replacement() source differs
    monkeypatch.undo()
    real_dockerfile = pages_deploy.dockerfile
    monkeypatch.setattr(pages_deploy, "dockerfile", lambda image: real_dockerfile(image) + b"X")
    assert pages_deploy.shipped_digest(SOURCE) != base  # Dockerfile template differs
