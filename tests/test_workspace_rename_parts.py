"""The workspace_rename parts: one namespace, one shipped program."""

import ast
import types

from plane_toolchain import workspace_rename as wr


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
