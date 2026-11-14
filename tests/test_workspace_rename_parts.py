"""The workspace_rename parts: one namespace, one shipped program."""

import ast
import types

import pytest

from plane_toolchain import workspace_rename as wr
from plane_toolchain import workspace_rename_share as share


def _part(name, source):
    module = types.ModuleType(name)
    exec(source, module.__dict__)  # noqa: S102 - synthetic part module
    return module


def test_adopt_refuses_decorated_functions():
    part = _part(
        "fake_decorated",
        "import functools\n"
        "def deco(f):\n"
        "    @functools.wraps(f)\n"
        "    def inner(*a): return f(*a)\n"
        "    return inner\n"
        "@deco\n"
        "def run(): pass\n",
    )
    with pytest.raises(TypeError, match="decorated"):
        share.adopt({}, (part,))


def test_adopt_refuses_part_classes_and_their_instances():
    with pytest.raises(TypeError, match="class"):
        share.adopt({}, (_part("fake_class", "class Thing: pass\n"),))
    instance = _part("fake_inst", "class Thing: pass\nthing = Thing()\n")
    del instance.Thing
    with pytest.raises(TypeError, match="instance"):
        share.adopt({}, (instance,))


def test_adopted_functions_are_not_the_part_originals():
    for part in wr.PARTS:
        for function in part.SHIPPED:
            if function.__module__ == part.__name__:
                assert getattr(wr, function.__name__) is not function


def test_patching_the_aggregate_reaches_part_functions(monkeypatch):
    said = []
    monkeypatch.setattr(wr, "say", said.append)
    monkeypatch.setattr(wr, "step_order", lambda phase: "patched")
    assert wr.apply_stop.__globals__["say"] == said.append
    assert wr.advance.__globals__["step_order"]("x") == "patched"


def test_each_shipped_function_is_in_the_remote_program_once():
    tree = ast.parse(wr.REMOTE)
    defined = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
    assert len(defined) == len(set(defined))
    assert {function.__name__ for function in wr.FUNCTIONS} <= set(defined)
    for part in wr.PARTS:
        assert part.SHIPPED
        for function in part.SHIPPED:
            assert defined.count(function.__name__) == 1


def test_part_functions_resolve_names_in_the_aggregate_namespace(monkeypatch):
    marker = object()
    monkeypatch.setattr(wr, "step_order", lambda phase: marker)
    # run_apply lives in another part than step_order: the patch must still reach it.
    assert wr.run_apply.__globals__ is vars(wr)
    assert wr.advance.__globals__["step_order"]("x") is marker


def test_adopted_functions_keep_their_source_for_shipping():
    import inspect

    from plane_toolchain import workspace_rename_receipt as receipt

    assert inspect.getsource(wr.advance) == inspect.getsource(receipt.advance)
    assert isinstance(wr.advance, types.FunctionType)
    assert wr.advance.__module__ == receipt.__name__
