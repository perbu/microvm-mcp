import concurrent.futures
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from microvm_mcp.errors import ServiceError
from microvm_mcp.images import Image
from microvm_mcp.service import Service
from microvm_mcp.store import Store


def image(image_id, **kw):
    return Image(image_id, Path("rootfs"), Path("vmlinux"), "0" * 64, "0" * 64, **kw)


class Backend:
    """Control-plane fake; deliberately incapable of executing code."""

    images = {"test-image": image("test-image")}
    default_image = "test-image"
    runtime_id = "test-runtime"

    def __init__(self):
        self.calls = []
        self.exec_args = []
        self.active = False
        self.jobs = {}
        self.broker_paths = {}
        self._broker_dir = tempfile.TemporaryDirectory()

    def create(self, vm):
        self.calls.append(("create", vm["id"]))

    def resume(self, vm, previous):
        self.calls.append(("resume", previous))

    def suspend(self, vm, mode, previous):
        self.calls.append(("suspend", mode))

    def destroy(self, vm):
        self.calls.append(("destroy", vm["id"]))

    def stop(self, vm):
        self.calls.append(("stop", vm["id"]))

    def guest(self, vm, op, args):
        self.calls.append(("guest", op))
        if op == "status":
            return {"active": self.active, "jobs": self.jobs}
        if op == "exec":
            self.active = True
            self.exec_args.append(args)
            self.jobs[args["job_id"]] = {"state": "running"}
            return {"job_id": args["job_id"]}
        return {"ok": True}

    def broker_listen(self, vm):
        self.calls.append(("listen_broker", vm["id"]))
        path = Path(self._broker_dir.name) / f"{vm['id']}.sock"
        path.unlink(missing_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        sock.listen(16)
        self.broker_paths[vm["id"]] = path
        return sock

    def connect(self, vm, port):
        # A canned echo peer, not a guest: greets, echoes, closes after EOF.
        self.calls.append(("connect", port))
        host, peer = socket.socketpair()

        def echo():
            with peer:
                peer.sendall(b"ready\n")
                if port == 5001:  # Download-style: send and close without reading.
                    return
                while data := peer.recv(65536):
                    peer.sendall(data)

        threading.Thread(target=echo, daemon=True).start()
        return host


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "state")
    token = store.issue("alice")
    bob = store.issue("bob")
    backend = Backend()
    return Service(store, backend), token, bob, backend


def create(env, **kw):
    service, token, _, _ = env
    return service.call(token, "vm_create", {"request_id": "create-1", **kw})["id"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("vm_status", {}),
        ("vm_exec", {"command": "true", "request_id": "x"}),
        ("vm_read_file", {"path": "x"}),
        ("vm_destroy", {}),
        ("vm_resume", {}),
        ("vm_suspend", {}),
        ("vm_job_output", {"job_id": "a" * 32}),
    ],
)
def test_other_workspace_cannot_access(env, name, args):
    vm = create(env)
    service, _, bob, backend = env
    before = list(backend.calls)
    with pytest.raises(ServiceError) as failure:
        service.call(bob, name, {"vm_id": vm, **args})
    assert failure.value.status == 404
    assert backend.calls == before
    assert service.call(bob, "vm_list", {}) == {"vms": []}


def test_rotation_and_revocation(env):
    vm = create(env)
    service, token, _, _ = env
    replacement = service.store.issue("alice", network=True, max_vms=10)
    assert service.call(replacement, "vm_status", {"vm_id": vm})["id"] == vm
    assert service.store.authenticate(replacement)["max_vms"] == 1
    service.store.revoke("alice")
    for credential in (token, replacement, "", "invalid"):
        with pytest.raises(ServiceError, match="credential"):
            service.call(credential, "vm_list", {})


def test_expired_credential_does_not_destroy_vm(env):
    vm = create(env)
    service, _, _, backend = env
    expired = service.store.issue("alice", ttl=0.001)
    time.sleep(0.005)
    with pytest.raises(ServiceError):
        service.call(expired, "vm_status", {"vm_id": vm})
    assert not any(c[0] == "destroy" for c in backend.calls)


def test_create_retries_and_concurrent_quota(env):
    service, token, _, backend = env
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        ids = list(pool.map(lambda _: create(env), range(4)))
    assert len(set(ids)) == 1
    assert sum(c[0] == "create" for c in backend.calls) == 1
    with pytest.raises(ServiceError, match="quota"):
        service.call(token, "vm_create", {"request_id": "different"})
    with pytest.raises(ServiceError, match="reused"):
        service.call(token, "vm_create", {"request_id": "create-1", "memory_mib": 512})


@pytest.mark.parametrize(
    "args",
    [{"owner": "bob"}, {"network": "on"}, {"vcpus": 8}, {"memory_mib": 8192}, {"memory_mib": True}],
)
def test_no_authority_in_arguments(env, args):
    with pytest.raises(ServiceError):
        create(env, **args)
    assert not env[3].calls


def test_busy_pause_force_and_resume(env):
    vm = create(env)
    service, token, _, backend = env
    args = {"vm_id": vm, "command": "long build", "request_id": "job-1"}
    job = service.call(token, "vm_exec", args)
    assert service.call(token, "vm_exec", args) == job
    assert backend.calls.count(("guest", "exec")) == 1
    with pytest.raises(ServiceError, match="Active jobs"):
        service.call(token, "vm_suspend", {"vm_id": vm})
    service.call(token, "vm_suspend", {"vm_id": vm, "force": True, "mode": "pause"})
    service.call(token, "vm_job_status", {"vm_id": vm, **job})
    assert not any(c[0] == "resume" for c in backend.calls)
    service.call(token, "vm_job_output", {"vm_id": vm, **job})
    assert ("resume", "paused") in backend.calls


def test_auto_suspend_only_idle_and_status_does_not_wake(env):
    vm = create(env, idle_seconds=1)
    service, token, _, backend = env
    service.vms[vm]["last_activity"] = time.time() - 10
    backend.active = True
    service.sweep()
    assert service.vms[vm]["state"] == "running"
    backend.active = False
    service.vms[vm]["last_activity"] = time.time() - 10
    service.sweep()
    assert service.call(token, "vm_status", {"vm_id": vm})["state"] == "hibernated"
    assert not any(c[0] == "resume" for c in backend.calls)


def test_restart_does_not_replay_uncertain_execution(env):
    vm = create(env)
    service, token, _, backend = env
    restarted = Service(service.store, backend)
    assert restarted.call(token, "vm_status", {"vm_id": vm})["state"] == "failed"
    with pytest.raises(ServiceError, match="not runnable"):
        restarted.call(token, "vm_exec", {"vm_id": vm, "command": "x", "request_id": "x"})


def test_hibernated_vm_survives_restart(env):
    vm = create(env)
    service, token, _, backend = env
    service.call(token, "vm_suspend", {"vm_id": vm})
    restarted = Service(service.store, backend)
    assert restarted.call(token, "vm_resume", {"vm_id": vm})["state"] == "running"


def test_snapshot_failure_never_claims_hibernation(env):
    vm = create(env)
    service, token, _, backend = env

    def fail(*args):
        raise OSError("disk full")

    backend.suspend = fail
    with pytest.raises(OSError):
        service.call(token, "vm_suspend", {"vm_id": vm})
    assert service.store.vms()[0]["state"] == "failed"


def test_uncertain_receipt_not_replayed(env):
    service, _, _, _ = env
    assert service.store.reserve("alice", "exec", "key", {"x": 1}) is None
    with pytest.raises(ServiceError, match="reconciliation"):
        service.store.reserve("alice", "exec", "key", {"x": 1})
