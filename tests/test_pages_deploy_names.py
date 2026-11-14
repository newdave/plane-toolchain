"""Pages deploy: reverted and failed container names never collide (PTC-104)."""

import pytest
from fake_docker import NOW
from plane_toolchain import pages_deploy
from support_pages_deploy import (
    BACKUP,
    DIGEST,
    LABEL,
    assert_original_restored,
    remote,
    restart,
    retained,
)
from support_pages_deploy import applied  # noqa: F401
from support_pages_deploy import interrupted  # noqa: F401


@pytest.mark.parametrize("kind", ["reverted", "failed"])
def test_unused_name_skips_taken_names_for_both_prefixes(kind):
    prefix = f"api-pages-{kind}-"
    taken = {f"{prefix}{NOW}", f"{prefix}{NOW}-1"}

    def probe(action, name):
        return name in taken

    assert pages_deploy.unused_name(prefix, probe, lambda: NOW) == f"{prefix}{NOW}-1-1"
    assert pages_deploy.unused_name(prefix, probe, lambda: NOW + 1) == f"{prefix}{NOW + 1}"


def test_revert_skips_an_existing_reverted_name(applied, monkeypatch):
    applied.add(f"api-pages-reverted-{NOW}", running=False)
    remote("revert", applied, monkeypatch)
    assert f"api-pages-reverted-{NOW}-1" in applied.containers
    assert_original_restored(applied)


def test_two_reverts_in_one_second_get_different_names(applied, monkeypatch):
    remote("revert", applied, monkeypatch)
    applied.add(BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    applied.containers["api"]["labels"][LABEL] = DIGEST
    remote("revert", applied, monkeypatch)
    assert {n for n in applied.containers if "-pages-reverted-" in n} == {
        f"api-pages-reverted-{NOW}",
        f"api-pages-reverted-{NOW}-1",
    }


def test_promotion_policy_ignores_suffixed_aside_and_picks_the_plain_suffixed(
    interrupted, monkeypatch
):
    live = f"api-pages-reverted-{NOW}-1"
    interrupted.containers.pop(f"api-pages-reverted-{NOW - 5}")
    interrupted.add(live, labels={LABEL: DIGEST}, running=False, RestartPolicy={"Name": "always"})
    interrupted.add(live + "-aside", running=False, RestartPolicy={"Name": "on-failure"})
    remote("revert", interrupted, monkeypatch)
    assert restart(interrupted, "api") == "always"


def test_interrupted_revert_uses_the_newest_suffixed_reverted_name(interrupted, monkeypatch):
    # Two reverts in one second: `<ts>` is stale, `<ts>-1` is the live derived and decides policy.
    stale, live = f"api-pages-reverted-{NOW}", f"api-pages-reverted-{NOW}-1"
    interrupted.containers.pop(f"api-pages-reverted-{NOW - 5}")
    interrupted.add(stale, labels={LABEL: "x"}, running=False, RestartPolicy={"Name": "no"})
    interrupted.add(live, labels={LABEL: DIGEST}, running=False, RestartPolicy={"Name": "always"})
    remote("revert", interrupted, monkeypatch)
    assert restart(interrupted, "api") == "always"
    assert set(retained(interrupted).values()) == {"no"}
