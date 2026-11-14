"""Pages deploy: refusal of legacy-named state."""

import subprocess
import pytest
from fake_docker import NOW, FakeDocker
from plane_toolchain import pages_deploy
from support_pages_deploy import (
    BACKUP,
    DIGEST,
    LEGACY_BACKUP,
    LEGACY_LABEL,
    REFUSAL,
    assert_untouched,
    deploy,
    legacy_backup_host,
    only_reads,
    remote,
    snapshot,
)
from support_pages_deploy import applied  # noqa: F401
from support_pages_deploy import host  # noqa: F401


@pytest.mark.parametrize("operation", ["check", "apply", "revert"])
@pytest.mark.parametrize("with_api", [True, False])
@pytest.mark.parametrize("shipped", [False, True])
def test_legacy_backup_container_refused_for_every_operation(
    operation, with_api, shipped, monkeypatch
):
    fake = legacy_backup_host(with_api)
    before = snapshot(fake)
    # Names sort: the container name precedes the label.
    names = LEGACY_BACKUP + (", " + LEGACY_LABEL if with_api else "")
    expected = REFUSAL.format(names)
    with pytest.raises(ValueError) as error:
        if shipped:
            remote(operation, fake, monkeypatch)
        else:
            deploy(operation, fake)
    assert str(error.value) == expected
    assert_untouched(fake, before)


def test_legacy_labelled_running_derived_refused_before_build():
    """A legacy-labelled derived must never be taken for an upstream original and rebuilt."""
    fake = FakeDocker()
    fake.add("api", labels={"owner": "kept", LEGACY_LABEL: DIGEST})
    before = snapshot(fake)
    with pytest.raises(ValueError) as error:
        deploy("apply", fake)
    assert str(error.value) == REFUSAL.format(LEGACY_LABEL)
    assert_untouched(fake, before)
    assert fake.containers["api"]["running"]


def test_legacy_labelled_neutral_backup_refused():
    fake = FakeDocker()
    fake.add(BACKUP, labels={LEGACY_LABEL: DIGEST}, running=False, RestartPolicy={"Name": "no"})
    before = snapshot(fake)
    with pytest.raises(ValueError) as error:
        deploy("apply", fake)
    assert str(error.value) == REFUSAL.format(LEGACY_LABEL)
    assert_untouched(fake, before)


def test_mixed_legacy_and_neutral_refused(applied):
    applied.add(
        LEGACY_BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"}
    )
    before = snapshot(applied)
    for operation in ("check", "apply", "revert"):
        with pytest.raises(ValueError) as error:
            deploy(operation, applied)
        assert str(error.value) == REFUSAL.format(LEGACY_BACKUP)
    assert_untouched(applied, before)


def test_neutral_host_unaffected(applied, host):
    assert deploy("check", applied) == "Pages image and API health verified (digest " + DIGEST + ")"
    assert deploy("apply", host).startswith("Pages derived image applied")
    assert deploy("revert", host, later=10).startswith("Original API container restored")


def test_remote_ships_tripwire_and_no_legacy_reads():
    compile(pages_deploy.REMOTE, "<remote>", "exec")
    assert "def legacy_state" in pages_deploy.REMOTE and "def refusal" in pages_deploy.REMOTE
    assert "def migrate" not in pages_deploy.REMOTE
    assert "def backup_for" not in pages_deploy.REMOTE
    assert "NEUTRAL_PAGES_LABEL = " in pages_deploy.REMOTE


@pytest.mark.parametrize("operation", ["check", "apply", "revert"])
@pytest.mark.parametrize("shipped", [False, True])
def test_one_shot_ps_failure_refuses(operation, shipped, applied, monkeypatch):
    applied.fail.append("ps")
    before = snapshot(applied)
    with pytest.raises(subprocess.CalledProcessError):
        if shipped:
            remote(operation, applied, monkeypatch)
        else:
            deploy(operation, applied)
    only_reads(applied)
    assert snapshot(applied) == before


@pytest.mark.parametrize("operation", ["check", "apply", "revert"])
@pytest.mark.parametrize("shipped", [False, True])
def test_one_shot_inspect_failure_on_listed_neutral_backup_refuses(
    operation, shipped, applied, monkeypatch
):
    applied.fail.append(("inspect", BACKUP))
    before = snapshot(applied)
    with pytest.raises(subprocess.CalledProcessError):
        if shipped:
            remote(operation, applied, monkeypatch)
        else:
            deploy(operation, applied)
    only_reads(applied)
    assert snapshot(applied) == before


def test_one_shot_inspect_failure_on_listed_api_refuses(applied):
    applied.fail.append(("inspect", "api"))
    with pytest.raises(subprocess.CalledProcessError):
        deploy("apply", applied)
    only_reads(applied)


def test_retired_legacy_labelled_reverted_container_stays_allowed(applied):
    applied.add(
        f"api-pages-reverted-{NOW}",
        labels={LEGACY_LABEL: DIGEST},
        running=False,
        RestartPolicy={"Name": "no"},
    )
    assert deploy("check", applied) == "Pages image and API health verified (digest " + DIGEST + ")"
