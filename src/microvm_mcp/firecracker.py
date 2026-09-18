"""The sole execution backend. No subprocess fallback for user commands."""

import fcntl
import hashlib
import http.client
import json
import os
import platform
import pwd
import re
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from .errors import ServiceError
from .network import Network, names

FICLONE = 0x40049409
MAX_FRAME = 512 * 1024


def clone(source, destination):
    """Fail closed on filesystems without reflinks; never use hardlinks."""
    src = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        dst = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.ioctl(dst, FICLONE, src)
            os.fsync(dst)
        finally:
            os.close(dst)
    finally:
        os.close(src)


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=120)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Firecracker:
    def __init__(self, config):
        self.config = config
        self.root = Path(config["state_dir"]).resolve()
        self.jails = self.root / "jails"
        self.binary = Path(config["firecracker"]).resolve()
        self.jailer = Path(config["jailer"]).resolve()
        self.kernel = Path(config["kernel"]).resolve()
        self.image = Path(config["rootfs"]).resolve()
        self.image_id = config["image_id"]
        with self.binary.open("rb") as src:
            self.runtime_id = hashlib.file_digest(src, "sha256").hexdigest()
        self.network = Network(
            config.get("uplink"), config.get("blocked_cidrs", ()), config.get("uid_base", 62000)
        )
        self.processes = {}
        self.diagnostics = {}
        self.uid_base = config.get("uid_base", 62000)
        self.cgroup_parent = "microvm-mcp"

    def preflight(self):
        if platform.machine() != "x86_64":
            raise ValueError("this first-pass guest image/kernel combination supports x86_64 only")
        if os.geteuid() != 0:
            raise ValueError("Firecracker jailer requires root; use a dedicated host service")
        with open("/dev/kvm", "rb+"):
            pass
        for path in (self.binary, self.jailer, self.kernel, self.image):
            if not path.is_file():
                raise ValueError(f"missing required file: {path}")
        for path in (self.binary, self.jailer):
            if not os.access(path, os.X_OK):
                raise ValueError(f"not executable: {path}")
        versions = []
        for path in (self.binary, self.jailer):
            version = subprocess.check_output([str(path), "--version"], text=True, timeout=5)
            match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", version)
            if not match:
                raise ValueError("cannot determine Firecracker/jailer version")
            versions.append(tuple(map(int, match.groups())))
        if versions[0] != versions[1] or versions[0] < (1, 17, 0):
            raise ValueError("matching Firecracker/jailer >= 1.17.0 required (vsock resume fixes)")
        if self.uid_base < 10000:
            raise ValueError("reserve an unused UID range >= 10000 for VM jails")
        for uid in range(self.uid_base, self.uid_base + self.config.get("max_vms", 16)):
            try:
                pwd.getpwuid(uid)
            except KeyError:
                continue
            raise ValueError(f"jail UID {uid} belongs to an existing host account")
        if not Path("/sys/fs/cgroup/cgroup.controllers").exists():
            raise ValueError("cgroup v2 is required")
        if (
            self.config.get("uplink")
            and Path("/proc/sys/net/ipv4/ip_forward").read_text().strip() != "1"
        ):
            raise ValueError("networking requires operator-enabled IPv4 forwarding")
        # Verify operator-pinned artifacts. No implicit latest-image lookup.
        for key, path in (("rootfs_sha256", self.image), ("kernel_sha256", self.kernel)):
            with path.open("rb") as src:
                digest = hashlib.file_digest(src, "sha256").hexdigest()
            if digest != self.config[key]:
                raise ValueError(f"artifact digest mismatch: {key}")
        probe = self.root / "reflink-probe"
        try:
            clone(self.image, probe)
        finally:
            probe.unlink(missing_ok=True)

    def directory(self, vm):
        return self.jails / self.binary.name / vm["id"] / "root"

    def storage_reservation(self, vm):
        # Worst-case private disk, two full memory generations during replacement,
        # and bounded kernel / metadata overhead. No thin-provisioning overcommit.
        return self.image.stat().st_size + 2 * vm["memory_mib"] * 1024**2 + 128 * 1024**2

    def api(self, vm, method, path, body):
        connection = UnixHTTP(self.directory(vm) / "api.sock")
        try:
            connection.request(method, path, json.dumps(body), {"Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(MAX_FRAME + 1)
            if response.status >= 300 or len(raw) > MAX_FRAME:
                raise ServiceError("vmm_error", "VMM rejected lifecycle operation", 502)
            return json.loads(raw) if raw else {}
        finally:
            connection.close()

    def launch(self, vm):
        directory = self.directory(vm)
        uid = self.uid_base + vm["slot"]
        # Jailer recreates its device nodes and executable on each launch. Preserve
        # the VM disk/checkpoint but remove the old jailer's launch scaffolding.
        if (directory / "dev").exists():
            shutil.rmtree(directory / "dev")
        (directory / self.binary.name).unlink(missing_ok=True)
        (directory / f"{self.binary.name}.pid").unlink(missing_ok=True)
        # API/vsock sockets from the terminated incarnation must not be reused.
        for path in directory.glob("*.sock*"):
            path.unlink()
        args = [
            str(self.jailer),
            "--id",
            vm["id"],
            "--exec-file",
            str(self.binary),
            "--uid",
            str(uid),
            "--gid",
            str(uid),
            "--chroot-base-dir",
            str(self.jails),
            "--netns",
            f"/run/netns/{names(vm)[0]}",
            "--cgroup-version",
            "2",
            "--parent-cgroup",
            self.cgroup_parent,
            "--cgroup",
            f"memory.max={(vm['memory_mib'] + 256) * 1024 * 1024}",
            "--cgroup",
            "memory.swap.max=0",
            "--cgroup",
            "pids.max=64",
            "--cgroup",
            f"cpu.max={vm['vcpus'] * 100000} 100000",
            "--resource-limit",
            "no-file=256",
            "--",
            "--api-sock",
            "/api.sock",
        ]
        # No inherited service secrets, stdin, or unbounded serial-console log file.
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={},
            close_fds=True,
        )
        self.processes[vm["id"]] = process
        self.diagnostics[vm["id"]] = bytearray()

        def drain():
            with process.stdout:
                while chunk := process.stdout.read1(4096):
                    tail = self.diagnostics[vm["id"]]
                    tail.extend(chunk)
                    del tail[:-65536]

        threading.Thread(target=drain, daemon=True).start()
        for _ in range(200):
            if process.poll() is not None:
                raise ServiceError("boot_failed", "Firecracker jailer exited during launch", 502)
            if (directory / "api.sock").exists():
                return
            time.sleep(0.025)
        raise ServiceError("boot_timeout", "VMM API did not become ready", 504)

    def create(self, vm):
        if shutil.disk_usage(self.root).free < self.storage_reservation(vm):
            raise ServiceError(
                "disk_full", "Insufficient space for a private disk and checkpoints", 409
            )
        directory = self.directory(vm)
        directory.mkdir(parents=True, mode=0o700)
        uid = self.uid_base + vm["slot"]
        clone(self.image, directory / "disk.ext4")
        shutil.copyfile(self.kernel, directory / "kernel")
        for path in (directory, directory / "disk.ext4", directory / "kernel"):
            os.chown(path, uid, uid)
        try:
            self.network.create(vm)
            self.launch(vm)
            self.api(
                vm,
                "PUT",
                "/machine-config",
                {"vcpu_count": vm["vcpus"], "mem_size_mib": vm["memory_mib"], "smt": False},
            )
            boot = "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda rw init=/sbin/microvm-init"
            if vm["network"] == "on":
                boot += f" mvm_ip=10.231.{vm['slot']}.2 mvm_gateway=10.231.{vm['slot']}.1"
            self.api(vm, "PUT", "/boot-source", {"kernel_image_path": "/kernel", "boot_args": boot})
            self.api(
                vm,
                "PUT",
                "/drives/rootfs",
                {
                    "drive_id": "rootfs",
                    "path_on_host": "/disk.ext4",
                    "is_root_device": True,
                    "is_read_only": False,
                },
            )
            self.api(vm, "PUT", "/vsock", {"guest_cid": 3 + vm["slot"], "uds_path": "/guest.sock"})
            if vm["network"] == "on":
                self.api(
                    vm,
                    "PUT",
                    "/network-interfaces/eth0",
                    {
                        "iface_id": "eth0",
                        "guest_mac": f"02:FC:00:00:00:{vm['slot']:02x}",
                        "host_dev_name": "tap0",
                    },
                )
            self.api(vm, "PUT", "/actions", {"action_type": "InstanceStart"})
            self.wait_guest(vm)
        except Exception:
            self.stop(vm)
            self.network.destroy(vm)
            raise

    def guest(self, vm, operation, args, *, timeout=10):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(self.directory(vm) / "guest.sock"))
            file = sock.makefile("rwb")
            try:
                file.write(b"CONNECT 1024\n")
                file.flush()
                if not file.readline(128).startswith(b"OK "):
                    raise ServiceError(
                        "guest_unavailable", "Guest control channel unavailable", 502
                    )
                file.write(json.dumps({"op": operation, "args": args}).encode() + b"\n")
                file.flush()
                raw = file.readline(MAX_FRAME + 1)
                if not raw.endswith(b"\n") or len(raw) > MAX_FRAME:
                    raise ServiceError("guest_protocol", "Invalid guest response", 502)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ServiceError("guest_protocol", "Invalid guest response", 502)
                if "error" in result:
                    # Never reflect arbitrary guest error text into service logs.
                    raise ServiceError("guest_error", "Guest rejected operation", 409)
                return result
            finally:
                file.close()

    def wait_guest(self, vm):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                self.guest(vm, "status", {}, timeout=1)
                return
            except (OSError, ValueError, ServiceError):
                time.sleep(0.1)
        raise ServiceError("boot_timeout", "Guest supervisor did not become ready", 504)

    def suspend(self, vm, mode, previous):
        if previous != "paused":
            self.api(vm, "PATCH", "/vm", {"state": "Paused"})
        if mode == "pause":
            return
        directory = self.directory(vm)
        if shutil.disk_usage(self.root).free < (vm["memory_mib"] + 64) * 1024**2:
            raise ServiceError("disk_full", "Insufficient checkpoint space", 409)
        # A checkpoint is usable only after the metadata state commits. Full snapshots
        # avoid diff-chain ambiguity. The private disk has no writers after pause/drain.
        for name in ("memory.next", "state.next"):
            (directory / name).unlink(missing_ok=True)
        self.api(
            vm,
            "PUT",
            "/snapshot/create",
            {
                "snapshot_type": "Full",
                "snapshot_path": "/state.next",
                "mem_file_path": "/memory.next",
            },
        )
        os.replace(directory / "memory.next", directory / "memory")
        os.replace(directory / "state.next", directory / "state")
        fd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self.stop(vm)
        self.network.destroy(vm)

    def resume(self, vm, previous):
        if previous == "paused":
            self.api(vm, "PATCH", "/vm", {"state": "Resumed"})
            self.wait_guest(vm)
            return
        self.network.create(vm)
        try:
            self.launch(vm)
            self.api(
                vm,
                "PUT",
                "/snapshot/load",
                {
                    "snapshot_path": "/state",
                    "mem_backend": {"backend_type": "File", "backend_path": "/memory"},
                    "enable_diff_snapshots": False,
                    "resume_vm": True,
                    "clock_realtime": True,
                },
            )
            self.wait_guest(vm)
        except Exception:
            self.stop(vm)
            self.network.destroy(vm)
            raise

    def stop(self, vm):
        process = self.processes.pop(vm["id"], None)
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
        # On service restart, use the service-owned cgroup, never an unverified PID.
        group = Path("/sys/fs/cgroup") / self.cgroup_parent / vm["id"]
        if group.exists():
            (group / "cgroup.kill").write_text("1")
            for _ in range(100):
                try:
                    group.rmdir()
                    break
                except OSError:
                    time.sleep(0.01)
            if group.exists():
                raise ServiceError("cleanup_failed", "VM cgroup could not be removed", 500)

    def destroy(self, vm):
        self.stop(vm)
        self.network.destroy(vm)
        directory = self.directory(vm).parent
        if directory.exists():
            shutil.rmtree(directory)
