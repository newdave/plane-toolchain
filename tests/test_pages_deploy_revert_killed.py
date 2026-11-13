"""Pages deploy: a revert killed after the original took the `api` name but before it started."""

from fake_docker import NOW, FakeDocker
from support_pages_deploy import (
    BACKUP,
    DIGEST,
    HEALTH,
    LABEL,
    LOOKUP,
    MUTATIONS,
    NOTE,
    remote,
    restart,
    running,
)

PARKED = f"api-pages-reverted-{NOW}"
PARKED_LISTING = ("ps", "-a", "--filter", "name=api-pages-reverted-", "--format", "{{.Names}}")


def killed_after_promotion(live, parked="always", up=False):
    """The original is `api` (policy `live`, running if `up`); the derived waits parked."""
    fake = FakeDocker()
    fake.add("api", labels={"owner": "kept"}, running=up, RestartPolicy={"Name": live})
    fake.add(
        PARKED,
        labels={"owner": "kept", LABEL: DIGEST},
        running=False,
        RestartPolicy={"Name": parked},
    )
    return fake


def test_revert_starts_a_stopped_original_left_by_a_killed_revert(monkeypatch, capsys):
    fake = killed_after_promotion("no", parked="always")
    remote("revert", fake, monkeypatch)
    assert capsys.readouterr().out == "Original API container restored\n"
    assert fake.calls == [
        *LOOKUP,
        ("inspect", BACKUP),
        PARKED_LISTING,  # is there evidence of an interrupted revert?
        PARKED_LISTING,  # which policy comes back?
        ("inspect", PARKED),
        ("update", "--restart=no", PARKED),
        ("update", "--restart=always", "api"),
        ("start", "api"),
        HEALTH,
    ]
    assert restart(fake, "api") == "always"  # the derived's live policy comes back
    assert restart(fake, PARKED) == "no"
    assert running(fake) == ["api"]


def test_revert_stopped_original_without_a_recoverable_policy_uses_the_default(monkeypatch, capsys):
    fake = killed_after_promotion("no", parked="no")
    remote("revert", fake, monkeypatch)
    assert capsys.readouterr().out == NOTE + "Original API container restored\n"
    assert restart(fake, "api") == "unless-stopped"
    assert running(fake) == ["api"]


def test_revert_leaves_a_stopped_original_alone_without_a_parked_derived(monkeypatch, capsys):
    fake = FakeDocker()
    fake.add("api", labels={"owner": "kept"}, running=False)  # stopped by the operator
    remote("revert", fake, monkeypatch)
    assert capsys.readouterr().out == "Original API container already restored\n"
    assert running(fake) == []
    assert not {call[0] for call in fake.calls} & MUTATIONS


def test_revert_running_original_with_a_parked_derived_is_already_restored(monkeypatch, capsys):
    fake = killed_after_promotion("always", up=True)
    remote("revert", fake, monkeypatch)
    assert capsys.readouterr().out == "Original API container already restored\n"
    assert not {call[0] for call in fake.calls} & MUTATIONS
