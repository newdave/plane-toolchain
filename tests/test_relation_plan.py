import asyncio
import io
import json
from contextlib import asynccontextmanager

import pytest
from plane_toolchain import relation_plan as rp
from plane_toolchain.cli import main
from plane_toolchain.cli_commands import ReadbackError, UsageError

PROJECT = "00000000-0000-0000-0000-000000000001"
OTHER = "00000000-0000-0000-0000-000000000002"
ENV = {
    "PLANE_ENABLED": "true",
    "PLANE_MCP_URL": "https://plane.example.com/mcp",
    "PLANE_WORKSPACE": "example-workspace",
    "PLANE_PROJECT": PROJECT,
    "PLANE_API_KEY": "plane_api_FAKEKEY123",
}
IDS = {f"ABC-{n}": f"00000000-0000-0000-0000-0000000001{n:02d}" for n in range(1, 8)}


class World:
    """Fake MCP: identifiers, per-item blocked_by sets, call log, optional misbehaviour."""

    def __init__(self, edges=(), foreign=(), unknown=(), drop_writes=False, fail_list=None):
        self.blocked_by = {i: set() for i in IDS.values()}
        for blocked, blocker in edges:
            self.blocked_by[IDS[blocked]].add(IDS[blocker])
        self.foreign, self.unknown = set(foreign), set(unknown)
        self.drop_writes, self.fail_list = drop_writes, fail_list
        self.calls = []

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "workitem":
            ident = args["workitem_identifier"]
            if ident in self.unknown:
                return {}
            project = OTHER if ident in self.foreign else PROJECT
            return {"id": IDS[ident], "project": project}
        assert name == "ce_workitem_relation"
        item = args["workitem_id"]
        if args["action"] == "list":
            if self.fail_list == item:
                raise RuntimeError("boom")
            blocking = [other for other in IDS.values() if item in self.blocked_by[other]]
            return {
                "dependencies": {
                    "blocked_by": [{"id": i} for i in sorted(self.blocked_by[item])],
                    "blocking": [{"id": i} for i in blocking],
                }
            }
        assert args["action"] == "create" and args["relation_type"] == "blocked_by"
        if not self.drop_writes:
            self.blocked_by[item].update(args["workitem_ids"])
        return {}

    def writes(self):
        return [c for c in self.calls if c[1].get("action") == "create"]


def pairs(*items):
    return [{"blocked": a, "blocker": b} for a, b in items]


def test_validate_normalises_and_dedupes():
    plan = rp.validate(pairs(("abc-1", "ABC-2"), ("ABC-1", "abc-2")))
    assert plan == [("ABC-1", "ABC-2")]


def test_validate_rejects_self_link():
    with pytest.raises(UsageError, match="itself"):
        rp.validate(pairs(("ABC-1", "abc-1")))


def test_validate_rejects_cycle():
    with pytest.raises(UsageError, match="cycle"):
        rp.validate(pairs(("ABC-1", "ABC-2"), ("ABC-2", "ABC-3"), ("ABC-3", "ABC-1")))


def test_validate_rejects_malformed_and_unknown_items():
    with pytest.raises(UsageError):
        rp.validate([{"blocked": "nope", "blocker": "ABC-1"}])
    with pytest.raises(UsageError):
        rp.validate([{"blocked": "ABC-1"}])
    with pytest.raises(UsageError, match="unknown"):
        rp.validate(pairs(("ABC-1", "ABC-2")), known={"ABC-1"})


def test_load_accepts_list_or_pairs_object(tmp_path):
    path = tmp_path / "p.json"
    path.write_text(json.dumps(pairs(("ABC-1", "ABC-2"))))
    assert rp.load(path) == pairs(("ABC-1", "ABC-2"))
    path.write_text(json.dumps({"pairs": pairs(("ABC-1", "ABC-2"))}))
    assert rp.load(path) == pairs(("ABC-1", "ABC-2"))
    path.write_text("not json")
    with pytest.raises(UsageError):
        rp.load(path)


def test_remote_refuses_unknown_and_cross_project():
    plan = rp.validate(pairs(("ABC-1", "ABC-2")))
    with pytest.raises(UsageError, match="unknown"):
        asyncio.run(rp.validate_remote(World(unknown={"ABC-2"}).call, PROJECT, plan))
    with pytest.raises(UsageError, match="one project"):
        asyncio.run(rp.validate_remote(World(foreign={"ABC-2"}).call, PROJECT, plan))


def test_remote_read_failure_stops_before_any_write():
    world = World(fail_list=IDS["ABC-1"])
    plan = rp.validate(pairs(("ABC-1", "ABC-2")))
    with pytest.raises(RuntimeError):
        asyncio.run(rp.validate_remote(world.call, PROJECT, plan))
    assert world.writes() == []


def test_remote_cycle_with_existing_relation_is_refused():
    world = World(edges=[("ABC-2", "ABC-1")])
    plan = rp.validate(pairs(("ABC-1", "ABC-2")))
    with pytest.raises(UsageError, match="cycle"):
        asyncio.run(rp.validate_remote(world.call, PROJECT, plan))
    assert world.writes() == []


def test_remote_returns_only_missing_links():
    world = World(edges=[("ABC-1", "ABC-2")])
    plan = rp.validate(pairs(("ABC-1", "ABC-2"), ("ABC-1", "ABC-3"), ("ABC-4", "ABC-3")))
    missing = asyncio.run(rp.validate_remote(world.call, PROJECT, plan))
    assert missing == {IDS["ABC-1"]: [IDS["ABC-3"]], IDS["ABC-4"]: [IDS["ABC-3"]]}
    assert world.writes() == []


def test_apply_writes_missing_paces_and_rereads():
    world = World()
    sleeps = []
    missing = {IDS["ABC-1"]: [IDS["ABC-2"], IDS["ABC-3"]], IDS["ABC-4"]: [IDS["ABC-3"]]}
    done = asyncio.run(rp.apply(world.call, PROJECT, missing, pause=0.5, sleep=sleeps.append))
    assert done == 3
    assert world.blocked_by[IDS["ABC-1"]] == {IDS["ABC-2"], IDS["ABC-3"]}
    assert len(world.writes()) == 2
    assert sleeps == [0.5]


def test_apply_unconfirmed_write_raises_readback():
    world = World(drop_writes=True)
    missing = {IDS["ABC-1"]: [IDS["ABC-2"]]}
    with pytest.raises(ReadbackError):
        asyncio.run(rp.apply(world.call, PROJECT, missing, 0, lambda s: None))


def cli(argv, world, tmp_path, plan):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))

    @asynccontextmanager
    async def connect(env=None):
        yield world.call

    out, err = io.StringIO(), io.StringIO()
    args = ["relation", "plan", str(path), *argv]
    code = main(args, env=ENV, root=tmp_path, connect=connect, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_cli_dry_run_writes_nothing(tmp_path):
    world = World()
    code, out, _ = cli([], world, tmp_path, pairs(("ABC-1", "ABC-2")))
    assert code == 0
    assert world.writes() == []
    report = json.loads(out)
    assert report["applied"] is False and report["missing"] == 1


def test_cli_apply_writes_and_is_idempotent(tmp_path):
    world = World()
    plan = pairs(("ABC-1", "ABC-2"), ("ABC-1", "ABC-2"))
    code, out, _ = cli(["--apply", "--pause", "0"], world, tmp_path, plan)
    assert code == 0 and json.loads(out)["applied"] is True
    assert len(world.writes()) == 1
    code, out, _ = cli(["--apply", "--pause", "0"], world, tmp_path, plan)
    assert code == 0 and json.loads(out)["missing"] == 0
    assert len(world.writes()) == 1


def test_cli_cycle_is_usage_error_before_connecting(tmp_path):
    world = World()
    plan = pairs(("ABC-1", "ABC-2"), ("ABC-2", "ABC-1"))
    code, _, err = cli(["--apply"], world, tmp_path, plan)
    assert code == 2 and "cycle" in err
    assert world.calls == []


def test_cli_cross_project_refused_without_writes(tmp_path):
    world = World(foreign={"ABC-2"})
    code, _, err = cli(["--apply"], world, tmp_path, pairs(("ABC-1", "ABC-2")))
    assert code == 2 and "one project" in err
    assert world.writes() == []
