import asyncio
import io
import json
from contextlib import asynccontextmanager

import anyio
import pytest
from plane_toolchain.cli import ReadbackError, UsageError, classify, environment, main

PROJECT = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
KEY = "plane_api_FAKEKEY123"
ENV = {
    "PLANE_ENABLED": "true",
    "PLANE_MCP_URL": "https://plane.example.com/mcp",
    "PLANE_WORKSPACE": "example-workspace",
    "PLANE_PROJECT": PROJECT,
    "PLANE_API_KEY": KEY,
}


class Fake:
    def __init__(self, results=None, error=None):
        self.calls = []
        self.entered = 0
        self.results = list(results or [])
        self.error = error

    @asynccontextmanager
    async def connect(self, env=None):
        self.entered += 1

        async def call(name, args):
            self.calls.append((name, args))
            if self.error:
                raise self.error
            return self.results.pop(0)

        yield call


def run(argv, fake, tmp, env=ENV):
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, env=env, root=tmp, connect=fake.connect, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_task_list_calls_workitem_list_without_pql_and_prints_json(tmp_path):
    result = {"results": [{"id": "a"}]}
    fake = Fake([result])
    code, out, _ = run(["task", "list"], fake, tmp_path)
    assert code == 0
    assert fake.calls == [("workitem", {"action": "list", "project_id": PROJECT})]
    assert json.loads(out) == result


def test_task_list_passes_paging(tmp_path):
    fake = Fake([{}])
    run(["task", "list", "--per-page", "5", "--cursor", "c"], fake, tmp_path)
    assert fake.calls[0][1] == {
        "action": "list",
        "project_id": PROJECT,
        "per_page": 5,
        "cursor": "c",
    }


def test_task_show_resolves_lowercase_identifier(tmp_path):
    fake = Fake([{"project": OTHER, "id": "w1"}, {"id": "w1"}])
    code, out, _ = run(["task", "show", "ptc-12"], fake, tmp_path)
    assert code == 0
    assert fake.calls == [
        ("workitem", {"action": "retrieve_by_identifier", "workitem_identifier": "PTC-12"}),
        ("workitem", {"action": "retrieve", "project_id": OTHER, "workitem_id": "w1"}),
    ]
    assert json.loads(out) == {"id": "w1"}


def test_task_show_uuid_uses_default_project(tmp_path):
    fake = Fake([{"id": "x"}])
    run(["task", "show", OTHER], fake, tmp_path)
    assert fake.calls == [
        ("workitem", {"action": "retrieve", "project_id": PROJECT, "workitem_id": OTHER})
    ]


def test_task_show_rejects_bad_reference_without_connecting(tmp_path):
    fake = Fake()
    code, out, err = run(["task", "show", "not-a-ref!"], fake, tmp_path)
    assert code == 2
    assert fake.entered == 0
    assert out == ""
    assert err.startswith("plane: ")


def test_project_option_overrides_default(tmp_path):
    fake = Fake([{}, {}])
    run(["--project", OTHER, "task", "list"], fake, tmp_path)
    run(["task", "list", "--project", OTHER], fake, tmp_path)
    assert [c[1]["project_id"] for c in fake.calls] == [OTHER, OTHER]


def test_bad_project_option_is_usage_error(tmp_path):
    fake = Fake()
    code, _, _ = run(["--project", "x", "task", "list"], fake, tmp_path)
    assert code == 2
    assert fake.entered == 0


def test_disabled_plane_is_configuration_error(tmp_path):
    fake = Fake()
    code, _, err = run(["task", "list"], fake, tmp_path, {**ENV, "PLANE_ENABLED": "false"})
    assert code == 3
    assert err.startswith("plane: ")
    assert fake.entered == 0


def test_unexpected_error_hides_key_and_traceback(tmp_path):
    fake = Fake(error=RuntimeError(f"boom {KEY}"))
    code, out, err = run(["task", "list"], fake, tmp_path)
    assert code == 4
    assert err == "plane: Plane MCP request failed; remote outcome must be checked\n"
    assert KEY not in out + err


def test_value_error_from_transport_is_exit_4(tmp_path):
    fake = Fake(error=ValueError("Required MCP tool unavailable"))
    code, _, err = run(["task", "list"], fake, tmp_path)
    assert code == 4
    assert err == "plane: Required MCP tool unavailable\n"


def test_argparse_usage_exit_code(tmp_path):
    assert run(["bogus"], Fake(), tmp_path)[0] == 2


def test_environment_reads_files_but_never_key_from_files(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/local.env").write_text(f"PLANE_PROJECT={PROJECT}\nPLANE_API_KEY=x\n")
    merged = environment({}, tmp_path)
    assert merged["PLANE_PROJECT"] == PROJECT
    assert "PLANE_API_KEY" not in merged
    assert environment({"PLANE_API_KEY": KEY}, tmp_path)["PLANE_API_KEY"] == KEY
    assert environment({"PLANE_PROJECT": OTHER}, tmp_path)["PLANE_PROJECT"] == OTHER


def test_connection_validates_the_passed_environment_not_os_environ(monkeypatch):
    import asyncio

    from plane_toolchain.connection import connection

    for name in ENV:
        monkeypatch.setenv(name, ENV[name])

    async def enter(env):
        async with connection(env):
            pass

    with pytest.raises(ValueError, match="disabled"):
        asyncio.run(enter({**ENV, "PLANE_ENABLED": "false"}))


def test_environment_enabled_falls_back_to_switch(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/local.env").write_text("PLANE_ENABLED=true\n")
    assert environment({}, tmp_path)["PLANE_ENABLED"] == "true"
    assert environment({"PLANE_ENABLED": "false"}, tmp_path)["PLANE_ENABLED"] == "false"


def grouped(error):
    """A connect whose body runs in a real anyio task group, like the transport."""

    class Grouped(Fake):
        @asynccontextmanager
        async def connect(self, env=None):
            self.entered += 1

            async def call(name, args):
                raise error

            async with anyio.create_task_group():
                yield call

    return Grouped()


def test_grouped_value_error_keeps_message_and_exit_4(tmp_path):
    fake = grouped(ValueError("Required MCP tool unavailable"))
    code, _, err = run(["task", "list"], fake, tmp_path)
    assert (code, err) == (4, "plane: Required MCP tool unavailable\n")


def test_grouped_readback_error_is_exit_5(tmp_path):
    code, _, err = run(["task", "list"], grouped(ReadbackError()), tmp_path)
    assert code == 5
    assert err.startswith("plane: write not confirmed")


def test_grouped_usage_error_is_exit_2(tmp_path):
    code, _, err = run(["task", "list"], grouped(UsageError("bad")), tmp_path)
    assert (code, err) == (2, "plane: bad\n")


def test_grouped_cancelled_error_is_exit_4_without_traceback(tmp_path):
    code, out, err = run(["task", "list"], grouped(asyncio.CancelledError()), tmp_path)
    assert code == 4
    assert err == "plane: Plane MCP request failed; remote outcome must be checked\n"
    assert "Traceback" not in out + err


def test_readback_outranks_value_error_in_a_group():
    group = BaseExceptionGroup("g", [ValueError("v"), ExceptionGroup("h", [ReadbackError()])])
    assert classify(group)[0] == 5


def test_missing_key_is_configuration_error_without_connecting(tmp_path):
    fake = Fake()
    env = {k: v for k, v in ENV.items() if k != "PLANE_API_KEY"}
    code, _, err = run(["task", "list"], fake, tmp_path, env)
    assert code == 3
    assert err == "plane: Missing or invalid PLANE_API_KEY\n"
    assert fake.entered == 0


def test_invalid_enabled_value_is_named(tmp_path):
    code, _, err = run(["task", "list"], Fake(), tmp_path, {**ENV, "PLANE_ENABLED": "maybe"})
    assert (code, err) == (3, "plane: Invalid PLANE_ENABLED\n")
