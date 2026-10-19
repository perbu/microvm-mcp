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

from . import images
from .errors import ServiceError
from .network import Network, names
from .streams import BROKER_PORT, CONTROL_PORT

FICLONE = 0x40049409
MAX_FRAME = 512 * 1024
LIFECYCLE_PROTOCOL = 1


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
        self.images, self.default_image = images.load(config)
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
        for path in (self.binary, self.jailer):
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
        # Verify every operator-pinned image. No implicit latest-image lookup.
        for image in self.images.values():
            for key, path in (("rootfs_sha256", image.rootfs), ("kernel_sha256", image.kernel)):
                if not path.is_file():
                    raise ValueError(f"missing required file: {path}")
                with path.open("rb") as src:
                    digest = hashlib.file_digest(src, "sha256").hexdigest()
                if digest != getattr(image, key):
                    raise ValueError(f"artifact digest mismatch: image {image.id} {key}")
            # Each image may sit on its own volume; every one must reflink into state.
            probe = self.root / "reflink-probe"
            try:
                clone(image.rootfs, probe)
            finally:
                probe.unlink(missing_ok=True)

    def directory(self, vm):
        return self.jails / self.binary.name / vm["id"] / "root"

    def disk_bytes(self, vm):
        if "disk_bytes" in vm:
            return vm["disk_bytes"]
        if vm.get("image") in self.images:
            return self.images[vm["image"]].rootfs.stat().st_size
        # A retained VM whose image left the configuration keeps its fixed-size disk.
        try:
            return (self.directory(vm) / "disk.ext4").stat().st_size
        except FileNotFoundError:
            return 0

    def storage_reservation(self, vm):
        # Worst-case private disk, two full memory generations during replacement,
        # and bounded kernel / metadata overhead. No thin-provisioning overcommit.
        return self.disk_bytes(vm) + 2 * vm["memory_mib"] * 1024**2 + 128 * 1024**2

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
        # The VM keeps private copies; resume never reads the image bundle again.
        image = self.images[vm["image"]]
        vm["disk_bytes"] = image.rootfs.stat().st_size
        directory = self.directory(vm)
        directory.mkdir(parents=True, mode=0o700)
        uid = self.uid_base + vm["slot"]
        clone(image.rootfs, directory / "disk.ext4")
        shutil.copyfile(image.kernel, directory / "kernel")
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
            self.sync_clock(vm)
            vm["lifecycle_protocol"] = LIFECYCLE_PROTOCOL
        except Exception:
            self.stop(vm)
            self.network.destroy(vm)
            raise

    def vsock(self, vm, port, timeout):
        """Host-initiated connection to a guest vsock port through Firecracker's UDS."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(str(self.directory(vm) / "guest.sock"))
            sock.sendall(f"CONNECT {port}\n".encode())
            # Byte-at-a-time: anything after the reply line is guest stream data.
            reply = bytearray()
            while not reply.endswith(b"\n") and len(reply) < 128:
                chunk = sock.recv(1)
                if not chunk:
                    break
                reply += chunk
            if not reply.startswith(b"OK ") or not reply.endswith(b"\n"):
                raise ConnectionRefusedError(f"guest vsock port {port} refused")
            return sock
        except BaseException:
            sock.close()
            raise

    def broker_listen(self, vm):
        """Listen where Firecracker connects guest-to-host vsock port 1023.

        The jailer sees uds_path `/guest.sock`, so a guest connection to the host
        arrives on `guest.sock_<port>` inside the jail. The jail user must be able
        to connect; this process keeps the accept fd.
        """
        path = self.directory(vm) / f"guest.sock_{BROKER_PORT}"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            path.unlink(missing_ok=True)
            sock.bind(str(path))
            uid = self.uid_base + vm["slot"]
            os.chown(path, uid, uid)
            os.chmod(path, 0o600)
            sock.listen(128)
            return sock
        except BaseException:
            sock.close()
            raise

    def connect(self, vm, port, *, timeout=5):
        try:
            sock = self.vsock(vm, port, timeout)
        except OSError:
            raise ServiceError("port_unavailable", "No guest listener on that port", 409) from None
        sock.settimeout(None)
        return sock

    def guest(self, vm, operation, args, *, timeout=10):
        try:
            sock = self.vsock(vm, CONTROL_PORT, timeout)
        except ConnectionRefusedError:
            raise ServiceError(
                "guest_unavailable", "Guest control channel unavailable", 502
            ) from None
        with sock:
            file = sock.makefile("rwb")
            try:
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
                status = self.guest(vm, "status", {}, timeout=1)
            except (OSError, ValueError, ServiceError):
                time.sleep(0.1)
                continue
            if status.get("lifecycle_protocol") != LIFECYCLE_PROTOCOL:
                raise ServiceError(
                    "incompatible_guest", "Rebuild the guest image for clock synchronization", 409
                )
            return
        raise ServiceError("boot_timeout", "Guest supervisor did not become ready", 504)

    def sync_clock(self, vm):
        # Send only host wall time, never workspace credentials. Jobs stay frozen
        # until the supervisor corrects CLOCK_REALTIME and their timeout budgets.
        result = self.guest(vm, "sync_clock", {"realtime_ns": time.time_ns()})
        if result.get("synchronized") is not True:
            raise ServiceError("guest_protocol", "Guest clock synchronization failed", 502)

    def suspend(self, vm, mode, previous):
        try:
            self._suspend(vm, mode, previous)
        except Exception:
            # A failed freeze/checkpoint must not leave an untracked runnable VM.
            self.stop(vm)
            self.network.destroy(vm)
            raise

    def _suspend(self, vm, mode, previous):
        if previous != "paused":
            result = self.guest(vm, "prepare_suspend", {})
            if result.get("frozen") is not True:
                raise ServiceError("guest_protocol", "Guest jobs did not freeze", 502)
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
        if vm.get("lifecycle_protocol") != LIFECYCLE_PROTOCOL:
            raise ServiceError(
                "incompatible_guest", "Recreate this VM with the clock-aware guest image", 409
            )
        try:
            if previous == "paused":
                self.api(vm, "PATCH", "/vm", {"state": "Resumed"})
                self.wait_guest(vm)
                self.sync_clock(vm)
                return
            self.network.create(vm)
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
                    # Advancing kvm-clock also advances monotonic timers. Correct
                    # CLOCK_REALTIME separately, independent of the clocksource.
                    "clock_realtime": False,
                },
            )
            self.wait_guest(vm)
            self.sync_clock(vm)
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
