"""Reversible derived-image deployment for an owner-managed Plane API.

Docker configuration stays inside the remote process. Never print it or save it to
an intermediate file. Retain the stopped original container as the rollback unit.
"""

import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


def replacement(original, image, live_url, digest):
    """Keep the existing opaque runtime configuration without exposing its values."""
    if original["HostConfig"].get("AutoRemove"):
        raise ValueError("AutoRemove prevents retaining a rollback container")
    body = copy.deepcopy(original["Config"])
    body["Image"] = image
    body["Env"] = [
        value for value in body.get("Env", []) if not value.startswith("CE_PLANE_LIVE_URL=")
    ]
    body["Env"].append("CE_PLANE_LIVE_URL=" + live_url)
    body["Labels"] = dict(body.get("Labels") or {}, **{"terrainlink.pages.source": digest})
    body["HostConfig"] = copy.deepcopy(original["HostConfig"])
    endpoints = {}
    for name, value in original["NetworkSettings"]["Networks"].items():
        if value.get("IPAMConfig"):
            raise ValueError("Static network addresses need a deployment-specific migration")
        endpoints[name] = {"Aliases": value.get("Aliases") or []}
    body["NetworkingConfig"] = {"EndpointsConfig": endpoints}
    return body


REMOTE = r"""
import hashlib,http.client,io,json,pathlib,socket,subprocess,sys,tarfile,time
payload=json.load(sys.stdin)
name=payload['container']
operation=payload['operation']
digest=payload['digest']
backup=name+'-before-terrainlink-pages'
def docker(*args):
    return subprocess.run(['docker',*args],check=True,capture_output=True).stdout
def inspect(target):
    return json.loads(docker('inspect',target))[0]
def healthy():
    for attempt in range(45):
        result=subprocess.run(['docker','exec',name,'python','-c',
            "import urllib.request; "
            "urllib.request.urlopen('http://127.0.0.1:8000/api/instances/',timeout=3)"],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if result.returncode==0:return
        time.sleep(1)
    raise ValueError('API health check failed')
current=inspect(name)
if operation=='check':
    if current['Config'].get('Labels',{}).get('terrainlink.pages.source')!=digest:
        raise ValueError('Pages image does not match source')
    healthy()
    print('Pages image and API health verified')
    sys.exit(0)
if operation=='revert':
    old=inspect(backup)
    if current['Config'].get('Labels',{}).get('terrainlink.pages.source')!=digest:
        raise ValueError('API changed; refusing rollback')
    docker('stop',name)
    docker('rename',name,name+'-pages-reverted-'+str(int(time.time())))
    docker('rename',backup,name)
    docker('start',name)
    healthy()
    print('Original API container restored; derived container retained stopped')
    sys.exit(0)
if operation!='apply':raise ValueError('Unknown operation')
if current['Config'].get('Labels',{}).get('terrainlink.pages.source')==digest:
    healthy()
    print('Pages image already applied; API healthy')
    sys.exit(0)
if subprocess.run(['docker','inspect',backup],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
    raise ValueError('Existing rollback container; reconcile before a new deployment')
source=payload['source'].encode()
base=json.loads(docker('image','inspect',current['Image']))[0]
if not base.get('RepoDigests'):raise ValueError('Running image has no immutable registry digest')
base_image=base['RepoDigests'][0]
image='terrainlink-plane-pages:'+digest[:16]
# Build from the exact running image; no credentials or runtime environment in context.
dockerfile=('FROM '+base_image+'\nCOPY plane_pages.py /code/plane/api/terrainlink_pages.py\n'
 'RUN python -c "from pathlib import Path; p=Path(\'/code/plane/api/urls/__init__.py\'); '
 'p.write_text(p.read_text()+'
 '\'\\nfrom plane.api.terrainlink_pages import routes\\nurlpatterns += routes()\\n\')"\n'
 'RUN python -m py_compile /code/plane/api/terrainlink_pages.py\n').encode()
stream=io.BytesIO()
with tarfile.open(fileobj=stream,mode='w') as archive:
    for filename,content in [('Dockerfile',dockerfile),('plane_pages.py',source)]:
        info=tarfile.TarInfo(filename)
        info.size=len(content)
        archive.addfile(info,io.BytesIO(content))
subprocess.run(['docker','build','-t',image,'-'],input=stream.getvalue(),check=True,
               stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
namespace={}
exec(payload['replacement'],namespace)
body=namespace['replacement'](current,image,payload['live_url'],digest)
class DockerHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.connect('/var/run/docker.sock')
def create():
    connection=DockerHTTP('localhost')
    connection.request('POST','/containers/create?name='+name,body=json.dumps(body),
                       headers={'Content-Type':'application/json'})
    response=connection.getresponse()
    response.read()
    if response.status!=201:raise ValueError('Derived API container creation failed')
    connection.close()
docker('stop',name)
docker('rename',name,backup)
try:
    create()
    docker('start',name)
    healthy()
except BaseException:
    if subprocess.run(['docker','inspect',name],
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:
        subprocess.run(['docker','stop',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        docker('rename',name,name+'-pages-failed-'+str(int(time.time())))
    docker('rename',backup,name)
    docker('start',name)
    raise
print('Pages derived image applied; API healthy; original container retained for rollback')
"""


def main():
    import inspect

    operation = sys.argv[1]
    host = os.environ["PLANE_MCP_SSH"]
    container = os.environ["PLANE_API_CONTAINER"]
    live_url = os.environ["PLANE_LIVE_URL"]
    if host.startswith("-") or not container.replace("-", "").replace("_", "").isalnum():
        raise ValueError("Invalid deployment destination")
    source = Path(__file__).with_name("pages_backend.py").read_text()
    payload = dict(
        operation=operation,
        container=container,
        live_url=live_url,
        source=source,
        digest=hashlib.sha256(source.encode()).hexdigest(),
        replacement="import copy\n" + inspect.getsource(replacement),
    )
    subprocess.run(  # noqa: S603 - validated owner host; fixed remote program
        ["ssh", host, "python3 -c " + shlex.quote(REMOTE)],  # noqa: S607 - system SSH
        input=json.dumps(payload),
        text=True,
        check=True,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(
            "Pages deployment failed; inspect container health "
            "before retrying. No runtime configuration logged.",
            file=sys.stderr,
        )
        sys.exit(1)
