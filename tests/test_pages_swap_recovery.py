"""The shared Pages swap recovery, tested alone (PTC-113)."""

import inspect

import pytest
from plane_toolchain import pages_deploy as pd


@pytest.fixture
def masks(monkeypatch):
    """Record hold_sigterm / release_sigterm so ordering against the recovery is visible."""
    log = []
    monkeypatch.setattr(pd, "hold_sigterm", lambda: log.append("hold") or "mask")
    monkeypatch.setattr(
        pd, "release_sigterm", lambda mask, failure=None: log.append(("release", mask, failure))
    )
    return log


def test_apply_and_revert_call_the_same_shared_routines():
    for function in (pd.apply, pd.revert):
        assert "recover_swap(" in inspect.getsource(function)
    assert "sigterm_held(" in inspect.getsource(pd.apply)
    for name in ("recover_swap", "sigterm_held", "undo_revert", "undo_apply", "retire_original"):
        assert f"\ndef {name}(" in pd.REMOTE


def test_recover_swap_holds_sigterm_around_the_undo_and_releases_with_the_error(masks):
    error = ValueError("swap failed")
    recover_swap_log = []
    pd.recover_swap(error, lambda: recover_swap_log.append(masks[:]))
    assert recover_swap_log == [["hold"]]
    assert masks == ["hold", ("release", "mask", error)]


def test_recover_swap_without_a_fallback_lets_an_undo_failure_replace_the_error(masks):
    def undo():
        raise OSError("undo failed")

    error = ValueError("swap failed")
    with pytest.raises(OSError, match="undo failed"):
        pd.recover_swap(error, undo)
    assert masks[-1] == ("release", "mask", error)


def test_recover_swap_runs_the_fallback_then_raises_the_original_error(masks):
    steps = []

    def undo():
        steps.append("undo")
        raise OSError("undo failed")

    error = ValueError("swap failed")
    with pytest.raises(ValueError) as caught:
        pd.recover_swap(error, undo, lambda: steps.append("fallback"))
    assert caught.value is error
    assert steps == ["undo", "fallback"]
    assert isinstance(caught.value.__context__, OSError)  # the recovery error stays as context
    assert masks[-1] == ("release", "mask", error)


def test_recover_swap_does_not_run_the_fallback_when_the_undo_works(masks):
    steps = []
    pd.recover_swap(ValueError("x"), lambda: steps.append("undo"), lambda: steps.append("fb"))
    assert steps == ["undo"]


def test_recover_swap_does_not_catch_a_baseexception_from_the_undo(masks):
    def undo():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        pd.recover_swap(ValueError("x"), undo, lambda: pytest.fail("fallback ran"))
    assert masks[-1][0] == "release"


def test_sigterm_held_releases_after_success_and_passes_the_failure_on(masks):
    pd.sigterm_held(lambda: masks.append("action"))
    assert masks == ["hold", "action", ("release", "mask", None)]

    masks.clear()
    boom = SystemExit(143)

    def action():
        raise boom

    with pytest.raises(SystemExit):
        pd.sigterm_held(action)
    assert masks == ["hold", ("release", "mask", boom)]
