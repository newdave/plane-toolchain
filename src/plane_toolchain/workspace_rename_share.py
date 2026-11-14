"""Let the workspace_rename parts run in one namespace, as they do on the host.

program() ships every function into one flat module on the host, so a call such as
`advance(...)` always means the one function of that name. Split across modules, each function
would instead resolve names in its own module, and `monkeypatch.setattr(workspace_rename, name,
...)` would stop reaching callers that live in another part. `adopt` rebuilds each part function
with the aggregate module's globals, so one namespace again decides every call. The function
code, and so its shipped source, is untouched.
"""

import types


def adopt(namespace, parts):
    """Fill `namespace` from `parts` and rebind each part's own functions to it; return them.

    Names a part imports or defines (constants, standard library modules) are added when the
    namespace lacks them. Each function defined in a part is copied with `namespace` as its
    globals and stored under its name. Returns {name: copy} of the functions rebound.
    """
    owned = {part.__name__ for part in parts}
    rebound = {}
    for part in parts:
        for name, value in vars(part).items():
            if name.startswith("__") or getattr(value, "__module__", None) in owned:
                continue
            namespace.setdefault(name, value)
    for part in parts:
        for name, value in vars(part).items():
            if isinstance(value, types.FunctionType) and value.__module__ == part.__name__:
                copy = types.FunctionType(
                    value.__code__,
                    namespace,
                    name,
                    value.__defaults__,
                    value.__closure__,
                )
                copy.__kwdefaults__ = value.__kwdefaults__
                copy.__dict__.update(value.__dict__)
                copy.__doc__ = value.__doc__
                copy.__qualname__ = value.__qualname__
                copy.__module__ = value.__module__
                copy.__annotations__ = value.__annotations__
                rebound[name] = copy
    namespace.update(rebound)
    return rebound
