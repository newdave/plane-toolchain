"""Reversible owner-approved MCP entry-point update; leave Docker settings intact."""

import json
import os
import subprocess
import sys
from pathlib import Path

REMOTE = r"""
import hashlib,json,pathlib,subprocess,sys
data=json.load(sys.stdin)
root=pathlib.Path(data['directory']).resolve(strict=True)
target=root/'server.py'
backup=root/'server.py.before-terrainlink-ce'
receipt=root/'terrainlink-ce-receipt.json'
if target.is_symlink() or not target.is_file():
    raise ValueError('Expected regular MCP entry point')
tracked=subprocess.run(['git','-C',str(root),'ls-files','--error-unmatch','server.py'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
if tracked.returncode==0:
    raise ValueError('Refusing to edit tracked host file')
current=target.read_bytes()
digest=lambda value:hashlib.sha256(value).hexdigest()
operation=data['operation']
desired=data['source'].encode()
compile(desired,str(target),'exec')
if operation=='check':
    if current!=desired: raise ValueError('MCP compatibility entry point differs')
    print('MCP entry point verified')
    sys.exit(0)
if operation=='apply':
    if current==desired:
        print('MCP compatibility entry point already applied')
        sys.exit(0)
    if backup.exists():
        if (current!=backup.read_bytes() and
            (not receipt.exists() or
             digest(current)!=json.loads(receipt.read_text())['installed'])):
            raise ValueError('Host entry point changed; preserve it and reconcile')
    else:
        if b'get_header_mcp' not in current or b'def ' in current or b'class ' in current:
            raise ValueError('Unexpected original entry point; review before replacing')
        backup.write_bytes(current)
        backup.chmod(target.stat().st_mode & 0o777)
    content=desired
elif operation=='revert':
    if not backup.exists() or not receipt.exists():
        raise ValueError('No compatibility backup available')
    previous=backup.read_bytes()
    if current==previous:
        print('Original MCP entry point already restored')
        sys.exit(0)
    if digest(current)!=json.loads(receipt.read_text())['installed']:
        raise ValueError('Host entry point changed; refusing overwrite')
    content=previous
else:
    raise ValueError('Expected apply, revert or check')
# Overwrite the existing inode so the read-only bind mount sees the update.
# This file contains code only; Docker credentials and configuration stay untouched.
target.write_bytes(content)
if operation=='apply':
    receipt.write_text(json.dumps(
        {'installed':digest(content),'original':digest(backup.read_bytes())}))
try:
    subprocess.run(['docker','restart',data['container']],check=True,stdout=subprocess.DEVNULL)
except BaseException:
    target.write_bytes(current)
    subprocess.run(['docker','restart',data['container']],check=False,stdout=subprocess.DEVNULL)
    raise
if target.read_bytes()!=content: raise ValueError('Entry point write not verified')
print('MCP entry point '+operation+' verified; verify authenticated MCP access separately')
"""


def main():
    operation = sys.argv[1]
    host = os.environ["PLANE_MCP_SSH"]
    directory = os.environ["PLANE_MCP_DIRECTORY"]
    container = os.environ.get("PLANE_MCP_CONTAINER", "plane-mcp")
    if host.startswith("-") or not directory.startswith("/") or container.startswith("-"):
        raise ValueError("Invalid MCP deployment destination")
    payload = {
        "operation": operation,
        "directory": directory,
        "container": container,
        "source": Path(__file__).with_name("ce_tools.py").read_text(),
    }
    import shlex

    subprocess.run(  # noqa: S603 - validated owner host; fixed remote program
        ["ssh", host, "python3 -c " + shlex.quote(REMOTE)],  # noqa: S607 - system SSH
        input=json.dumps(payload),
        text=True,
        check=True,
    )


if __name__ == "__main__":
    main()
