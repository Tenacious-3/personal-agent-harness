"""Build and verify isolated runtime/acceptance images with synthetic state.

Requires a running Docker-compatible Engine. No host state, Docker socket or
real credentials are mounted. Only uniquely named fixtures created here are
removed on exit; existing application containers and volumes are untouched.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
import uuid


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-container-token-at-least-24-characters"
SECRET = "synthetic-container-key-at-least-24-characters"


def docker(*args, capture=False, check=True):
    return subprocess.run(["docker", *args], cwd=ROOT, text=True, check=check,
                          stdout=subprocess.PIPE if capture else None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="personal-agent-harness:acceptance-ci")
    parser.add_argument("--build", action="store_true", help="Build both pinned images before testing")
    parser.add_argument("--bind-database", action="store_true", help="Verify a separate native Linux database bind across recreation")
    options = parser.parse_args()
    if options.bind_database and sys.platform!='linux':
        parser.error('Run bind-database acceptance inside native Linux/WSL')
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,200}", options.image) or "@" in options.image:
        parser.error("Use a local image name/tag, not a digest reference")
    try:
        docker("version", "--format", "{{.Server.Version}}", capture=True)
    except (OSError, subprocess.CalledProcessError):
        parser.exit(2, "Docker CLI and a running Linux-container Engine are required; see docs/DOCKER.md.\n")
    if docker("info", "--format", "{{.OSType}}", capture=True).stdout.strip() != "linux":
        parser.exit(2, "Switch the Docker Engine to Linux containers before running this gate.\n")
    runtime_image = options.image + "-runtime"
    if options.build:
        docker("build", "--platform", "linux/amd64", "--target", "runtime", "-t", runtime_image, ".")
        docker("build", "--platform", "linux/amd64", "--target", "acceptance", "-t", options.image, ".")
    docker("run", "--rm", "--network", "none", "--entrypoint", "python", runtime_image, "-c",
           "import importlib.util,os,pathlib; assert os.getuid()!=0; "
           "assert not pathlib.Path('/app/scripts/container-fixture.py').exists(); "
           "assert not pathlib.Path('/app/backend/tests').exists(); "
           "assert importlib.util.find_spec('harness') is not None; "
           "assert not pathlib.Path('/app/.dev.vars').exists(); print('Runtime image is non-root and contains no test fixture or local config')")
    docker("run", "--rm", "--init", "--network", "none", "--read-only", "--shm-size", "256m",
           "--tmpfs", "/tmp:rw,nosuid,size=268435456", "--tmpfs", "/test-work:rw,nosuid,uid=10001,gid=10001,size=268435456",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--pids-limit", "512",
           "--memory", "2g", options.image)

    suffix = uuid.uuid4().hex[:16]
    container = "harness-container-smoke-" + suffix
    volume = "harness-container-state-" + suffix
    bound_database=tempfile.TemporaryDirectory(prefix='harness-bind-database-') if options.bind_database else None
    if bound_database: Path(bound_database.name).chmod(0o777)  # Isolated fixture data; app remains UID 10001.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, body=None, *, authenticated=True, expected=200, host="127.0.0.1:8787"):
        headers = {"Host": host}
        if authenticated: headers["Authorization"] = "Bearer " + TOKEN
        if body is not None: headers["Content-Type"] = "application/json"
        payload = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{port}" + path, data=payload, headers=headers)
        try:
            response = opener.open(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            statuses = expected if isinstance(expected, tuple) else (expected,)
            if response.status not in statuses:
                raise AssertionError(f"{path}: expected HTTP {expected}, got {response.status}")
            return json.loads(raw) if raw else None

    def wait_ready():
        deadline = time.monotonic() + 45
        while True:
            try:
                request("/health", authenticated=False)
                return
            except (OSError, AssertionError):
                if time.monotonic() >= deadline: raise
                time.sleep(.2)

    def start():
        database_args=['--mount',f'type=bind,src={bound_database.name},dst=/database',
                       '--env','ASSISTANT_DATABASE_DIR=/database'] if bound_database else []
        docker("run", "--detach", "--name", container, "--init", "--read-only", "--shm-size", "256m",
               "--publish", f"127.0.0.1:{port}:8787", "--mount", f"type=volume,src={volume},dst=/data/.assistant",
               "--tmpfs", "/tmp:rw,nosuid,size=268435456", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges:true", "--pids-limit", "512", "--memory", "2g",
               "--env", "HARNESS_TOKEN=" + TOKEN, "--env", "CREDENTIAL_KEY=" + SECRET,
               "--env", "OPENAI_API_KEY=synthetic-container-no-network", *database_args, options.image,
               "python", "-u", "/app/scripts/container-fixture.py", capture=True)
        wait_ready()

    def verify_persisted(memory_id, approval_id, schedule_id, document_id):
        if bound_database:
            assert (Path(bound_database.name)/'assistant.sqlite').is_file()
            docker('exec',container,'python','-c',"from pathlib import Path; assert not Path('/data/.assistant/assistant.sqlite').exists()")
        assert any(item["id"] == memory_id for item in request("/api/memories")["memories"])
        assert len(request("/api/history")["messages"]) == 2
        document=request(f"/api/memory-documents/{document_id}")
        assert document['content']=='Synthetic sourced container document'
        assert document['revision']==1 and document['provenance']=='user_reviewed'
        assert 'Synthetic sourced container document' in request('/api/files/read?path=memory/agent/container/state.md')['content']
        assert any(item['id']==document_id for item in request('/api/recall/search',{'query':'Synthetic sourced container document'})['results'])
        approval = next(item for item in request("/api/approvals")["approvals"] if item["id"] == approval_id)
        assert approval["status"] == "completed"
        schedule = next(item for item in request("/api/schedules")["schedules"] if item["id"] == schedule_id)
        assert schedule["status"] == "completed"
        docker("exec", container, "python", "-c",
               "from pathlib import Path; p=Path('/data/.assistant/browser-profile/.personal-assistant-profile'); "
               "assert p.is_file(); assert p.read_text()=='personal-assistant-harness/browser-profile/v1'")

    fixture_url = 'https://93.184.215.14/'

    def verify_browser_login():
        tab = request('/api/browser/navigate', {'url': fixture_url})
        snapshot = request('/api/browser/snapshot?tabId=' + tab['id'])
        assert 'Fixture signed in' in snapshot['text']
        request('/api/browser/close', {})

    try:
        docker("volume", "create", volume, capture=True)
        start()
        request("/api/status", authenticated=False, expected=401)
        request("/health", authenticated=False, host="evil.example:8787", expected=400)
        chat = request("/api/chat", {"message": "Synthetic container chat"})
        assert chat["message"] == "Synthetic container acceptance result"
        memory = request("/api/memories", {"text": "Synthetic container memory"}, expected=201)
        document=request('/api/memory-documents',{'path':'memory/agent/container/state.md',
            'content':'Synthetic sourced container document','sourceIds':['memory:'+memory['id']],
            'privacyRevision':request('/api/memory-documents')['privacyRevision']},expected=201)
        approval = request("/api/actions", {"kind": "skill.save", "summary": "Synthetic exact procedure",
            "payload": {"name": "container-acceptance", "content": "# Synthetic approved procedure"}}, expected=201)
        decision = {"decision": "approved", "expectedDigest": approval["digest"]}
        assert request(f"/api/approvals/{approval['id']}/decision", decision)["status"] == "completed"
        request(f"/api/approvals/{approval['id']}/decision", decision, expected=409)
        schedule = request("/api/schedules", {"prompt": "Synthetic due container task", "runAt": 1}, expected=201)
        deadline = time.monotonic() + 15
        while not any(t["id"].startswith("schedule-" + schedule["id"] + "-") and t["status"] == "completed"
                      for t in request("/api/tasks")["tasks"]):
            if time.monotonic() > deadline: raise AssertionError("Synthetic scheduled task did not finish")
            time.sleep(.1)
        request("/api/browser/launch", {"visible": False})
        request("/api/browser/navigate", {"url": "http://127.0.0.1/private-destination"}, expected=400)
        tab = request('/api/browser/navigate', {'url': fixture_url})
        lease = request('/api/browser/manual/start', {'tabId': tab['id']})['lease']
        request('/api/browser/tabs', expected=400)
        for command in ({'kind': 'click', 'x': 120, 'y': 110},
                        {'kind': 'text', 'value': 'fixture-only-value'}, {'kind': 'key', 'value': 'Enter'}):
            frame = request('/api/browser/manual/frame', {'lease': lease})
            request('/api/browser/manual/input', {'lease': lease, 'frame': frame['frame'], **command})
        deadline = time.monotonic() + 10
        while request('/api/browser/manual/frame', {'lease': lease}, expected=(200, 409)).get('title') != 'Fixture signed in':
            if time.monotonic() >= deadline:
                raise AssertionError('Synthetic sign-in did not reach the signed-in page')
            time.sleep(.1)
        request('/api/browser/manual/end', {'lease': lease})
        verify_browser_login()
        verify_persisted(memory["id"], approval["id"], schedule["id"],document['id'])
        docker("restart", container, capture=True)
        wait_ready()
        verify_persisted(memory["id"], approval["id"], schedule["id"],document['id'])
        docker("stop", "--time", "15", container, capture=True)
        docker("rm", container, capture=True)
        start()
        verify_persisted(memory["id"], approval["id"], schedule["id"],document['id'])
        verify_browser_login()
        print("Container auth, approval/replay, scheduling, SQLite persistence, and dedicated sign-in across recreation passed")
    except BaseException:
        docker("logs", "--tail", "80", container, check=False)
        raise
    finally:
        # These identifiers were generated in this invocation, never user input.
        if not container.startswith("harness-container-smoke-") or not volume.startswith("harness-container-state-"):
            raise RuntimeError("Refusing to clean an unowned Docker fixture")
        docker("rm", "--force", container, capture=True, check=False)
        docker("volume", "rm", volume, capture=True, check=False)
        if bound_database: bound_database.cleanup()


if __name__ == "__main__":
    main()
