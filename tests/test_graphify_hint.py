"""graphify PreToolUse hint fires only for searches when a graph exists."""

import importlib.util
import io
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/graphify-hint.py"
spec = importlib.util.spec_from_file_location("graphify_hint", SCRIPT)
hint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hint)


def run(event, root, capsys):
    assert hint.main(io.StringIO(json.dumps(event)), root) == 0
    return capsys.readouterr().out


@pytest.fixture
def graph(tmp_path):
    (tmp_path / "graphify-out").mkdir()
    (tmp_path / "graphify-out/graph.json").write_text("{}")
    return tmp_path


@pytest.mark.parametrize(
    "event",
    [
        {"tool_name": "Bash", "tool_input": {"command": "grep -rn foo src"}},
        {"tool_name": "Bash", "tool_input": {"command": "cd src && rg foo"}},
        {"tool_name": "Bash", "tool_input": {"command": "find . -name '*.py'"}},
        {"tool_name": "Grep", "tool_input": {"pattern": "foo"}},
        {"tool_name": "Glob", "tool_input": {"pattern": "**/*.py"}},
    ],
)
def test_searches_get_hint(event, graph, capsys):
    out = json.loads(run(event, graph, capsys))
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "graphify query" in out["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize(
    "event",
    [
        {"tool_name": "Bash", "tool_input": {"command": "git status"}},
        {"tool_name": "Bash", "tool_input": {"command": "uv run pytest tests/test_finder.py"}},
        {"tool_name": "Read", "tool_input": {"file_path": "x"}},
        {"tool_name": "Bash"},
        [],
    ],
)
def test_other_tools_silent(event, graph, capsys):
    assert run(event, graph, capsys) == ""


def test_silent_without_graph_or_on_bad_input(tmp_path, capsys):
    assert run({"tool_name": "Grep"}, tmp_path, capsys) == ""
    assert hint.main(io.StringIO("not json"), tmp_path) == 0
    assert capsys.readouterr().out == ""
