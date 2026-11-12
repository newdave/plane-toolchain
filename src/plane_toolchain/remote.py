"""Docker seams and program assembly for deployment code run on owner hosts.

Functions shipped with `program` run as `python3 -c` over SSH: keep them stdlib-only and
never print runtime configuration.
"""

import inspect
import subprocess


def docker(*args, **kw):
    """Run the Docker CLI; raise on failure unless check=False; return stdout bytes.

    Output is captured unless the caller routes stdout or stderr itself.
    """
    kw.setdefault("check", True)
    if "stdout" not in kw and "stderr" not in kw:
        kw["capture_output"] = True
    return subprocess.run(["docker", *args], **kw).stdout or b""  # noqa: S603,S607


def docker_ok(*args):
    """Run the Docker CLI without raising or output; return whether it succeeded."""
    result = subprocess.run(  # noqa: S603,S607
        ["docker", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return result.returncode == 0


def program(imports, functions, entry):
    """Assemble a self-contained remote program from function source and an entry stub."""
    parts = ["import " + ",".join(imports)]
    parts += [inspect.getsource(function) for function in functions]
    return "\n".join(parts + [entry])
