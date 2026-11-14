"""Kill the Pages remote program at every docker call, then prove the next run converges.

A SIGKILL leaves the host exactly as it was at that instant. For each call index of apply and of
revert, and both before and after the call took effect, the program dies there. A second run of
the same operation must then finish, and the opposite operation must work afterwards, leaving
one running API container, no stranded aside or derived state and sane restart policies.
"""

import pytest
from fake_docker import FakeDocker, Killed
from support_pages_deploy import BACKUP, DIGEST, LABEL, remote, restart, retained, running


LIVE = "always"  # not the default, so a policy lost in a window shows
DEFAULT = "unless-stopped"  # the documented fallback (with a NOTE) when the live policy is lost
# Kill points where the live policy is unrecoverable because the kill lands between the rename
# that retires it and the update that records it. The next run falls back to DEFAULT.
WINDOWS = {
    ("apply", 7, "after"),
    ("apply", 8, "before"),
    ("revert", 7, "after"),
    ("revert", 8, "before"),
    ("revert", 8, "after"),
    ("revert", 9, "before"),
}


def fresh():
    fake = FakeDocker()
    fake.add("api", labels={"owner": "kept"}, RestartPolicy={"Name": LIVE})
    return fake


def applied():
    fake = FakeDocker()
    fake.add(BACKUP, labels={"owner": "kept"}, running=False, RestartPolicy={"Name": "no"})
    fake.add("api", labels={"owner": "kept", LABEL: DIGEST}, RestartPolicy={"Name": LIVE})
    return fake


START = {"apply": fresh, "revert": applied}
OTHER = {"apply": "revert", "revert": "apply"}
DERIVED_AFTER = {"apply": True, "revert": False}
CALLS = {"apply": 13, "revert": 13}  # docker calls in one clean run; the tripwire below checks


def assert_converged(fake, derived, policy=LIVE):
    """One running API, nothing stranded, the right image on it, retained containers off."""
    assert running(fake) == ["api"]
    assert fake.containers["api"]["labels"].get(LABEL) == (DIGEST if derived else None)
    assert restart(fake, "api") == policy
    assert (BACKUP in fake.containers) == derived  # the original is parked only while derived
    assert set(retained(fake).values()) == {"no"}
    assert not [n for n in fake.containers if n.endswith("-aside")]
    # Retired and failed containers may remain (prune removes them) but only stopped, which
    # `running == ["api"]` and the policy check above prove.
    assert all(n.startswith("api-") for n in retained(fake))


@pytest.mark.parametrize("operation", ["apply", "revert"])
def test_the_harness_covers_every_docker_call_of_a_clean_run(operation, monkeypatch):
    fake = START[operation]()
    state = fake.kill_at(10**6, "before")  # counts calls, never kills
    remote(operation, fake, monkeypatch)
    assert state["count"] == CALLS[operation]


@pytest.mark.parametrize(
    ("operation", "index", "when"),
    [
        (operation, index, when)
        for operation, count in CALLS.items()
        for index in range(count)
        for when in ("before", "after")
    ],
)
def test_killed_program_converges_on_the_next_run(operation, index, when, monkeypatch):
    fake = START[operation]()
    fake.kill_at(index, when)
    with pytest.raises(Killed):
        remote(operation, fake, monkeypatch)
    del fake.execute, fake.create  # the replacement process talks to the host directly again
    policy = DEFAULT if (operation, index, when) in WINDOWS else LIVE
    remote(operation, fake, monkeypatch)
    assert_converged(fake, DERIVED_AFTER[operation], policy)
    remote(OTHER[operation], fake, monkeypatch)
    assert_converged(fake, DERIVED_AFTER[OTHER[operation]], policy)


@pytest.mark.parametrize(
    ("operation", "index"),
    [(operation, index) for operation, count in CALLS.items() for index in range(count)],
)
def test_one_failing_call_leaves_one_running_api_and_the_next_run_converges(
    operation, index, monkeypatch
):
    fake = START[operation]()
    state = fake.kill_at(index, "fail")
    try:
        remote(operation, fake, monkeypatch)  # a tolerated failure (e.g. a policy update) is fine
    except Exception:  # noqa: S110 - the failure is the point; the host state is what counts
        pass
    assert len(running(fake)) == 1 and fake.containers["api"]["running"]
    del fake.execute, fake.create
    # A failed policy update is a documented warning that a re-run does not repair, so only the
    # single running API is asserted for it; every other failure must leave a converged host.
    policy_warned = state["failed"][0] == "update"
    remote(operation, fake, monkeypatch)
    if policy_warned:
        assert running(fake) == ["api"]
    else:
        assert_converged(fake, DERIVED_AFTER[operation])
    remote(OTHER[operation], fake, monkeypatch)
    if policy_warned:
        assert running(fake) == ["api"]
    else:
        assert_converged(fake, DERIVED_AFTER[OTHER[operation]])


@pytest.mark.parametrize(
    ("operation", "index"),
    [(operation, index) for operation, count in CALLS.items() for index in range(count)],
)
def test_a_call_that_reports_failure_but_took_effect_leaves_one_running_api(
    operation, index, monkeypatch
):
    fake = START[operation]()
    state = fake.kill_at(index, "failed-took-effect")
    try:
        remote(operation, fake, monkeypatch)
    except Exception:  # noqa: S110 - the failure is the point; the host state is what counts
        pass
    assert len(running(fake)) <= 1  # never two APIs, whatever the call did
    del fake.execute, fake.create
    policy_warned = state["failed"][0] == "update"
    remote(operation, fake, monkeypatch)
    if policy_warned:
        assert running(fake) == ["api"]
    else:
        assert_converged(fake, DERIVED_AFTER[operation])
