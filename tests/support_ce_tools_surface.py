"""Capture the MCP tool surface ce_tools.main() registers, using a fake FastMCP registry.

No plane_mcp needed: the three modules main() imports are stubbed in sys.modules.
"""

import inspect
import sys
import types
from typing import Any

from pydantic import create_model


def _schema(fn):
    fields = {}
    for name, param in inspect.signature(fn).parameters.items():
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (param.annotation, default)
    return create_model("Args", **fields).model_json_schema()


class FakeServer:
    def __init__(self):
        self.tools = {}

    def tool(self, name, description=None):
        def register(fn):
            self.tools[name] = (description, fn)
            return fn

        return register

    def run(self, **kwargs):
        pass


def capture(main):
    """Run main() against stubs; return {name: {description, parameters, returns}}."""
    server = FakeServer()
    stubs = {
        "plane.models.work_items": types.SimpleNamespace(CreateWorkItemRelation=object),
        "plane_mcp.client": types.SimpleNamespace(get_plane_client_context=lambda: None),
        "plane_mcp.server": types.SimpleNamespace(get_header_mcp=lambda: server),
    }
    saved = {k: sys.modules.get(k) for k in stubs}
    sys.modules.update(stubs)
    try:
        main()
    finally:
        for key, old in saved.items():
            if old is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = old
    surface: dict[str, Any] = {}
    for name, (description, fn) in server.tools.items():
        surface[name] = {
            "description": description if description is not None else inspect.getdoc(fn),
            "parameters": _schema(fn),
            "returns": str(inspect.signature(fn).return_annotation),
        }
    return surface
