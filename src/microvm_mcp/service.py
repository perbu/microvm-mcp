import hashlib
import hmac
import logging
import secrets
import threading
import time
import uuid

from jsonschema import Draft202012Validator

from .broker import MAX_ATTACHED, BrokerAcceptor, BrokerPipe
from .catalog import tools
from .errors import ServiceError
from .streams import RESERVED_PORTS, Stream, port_ranges

LOG = logging.getLogger(__name__)
# How often the idle sweep asks the guest whether broker jobs are still running.
BROKER_RECHECK_SECONDS = 5


class Service:
    def __init__(
        self,
        store,
        backend,
        *,
        max_vms=16,
        max_memory_mib=16384,
        max_storage_bytes=64 * 1024**3,
        stream_ports=((1025, 65535),),
        max_streams=32,
        max_streams_per_workspace=8,
        max_streams_per_vm=4,
        stream_rate_bytes=32 * 1024**2,
    ):
        if not 1 <= max_vms <= 64:
            raise ValueError("max_vms must be between 1 and 64")
        if not 0 <= max_streams_per_vm <= max_streams_per_workspace <= max_streams <= 256:
            raise ValueError("stream limits must satisfy per-VM <= per-workspace <= host <= 256")
        if type(stream_rate_bytes) is not int or stream_rate_bytes < 0:
            raise ValueError("stream_rate_bytes must be a non-negative integer (0: unlimited)")
        self.store, self.backend, self.max_vms = store, backend, max_vms
        self.max_memory_mib = max_memory_mib
        self.max_storage_bytes = max_storage_bytes
        self.stream_ports = port_ranges(stream_ports)
        self.max_streams = max_streams
        self.max_streams_per_workspace = max_streams_per_workspace
        self.max_streams_per_vm = max_streams_per_vm
        self.stream_rate_bytes = stream_rate_bytes
        # vm_id -> open Stream objects. Mutated only under self.guard.
        self.streams = {}
        # vm_id -> {job_id: BrokerPipe}. Token hashes and route secrets are memory-only:
        # never receipts, status, or logs. Mutated only under self.guard.
        self.brokers = {}
        self.broker_listeners = {}
        self.broker_checked = {}
        self.guard = threading.RLock()
        self.vms = {v["id"]: v for v in store.vms()}
        self.locks = {key: threading.RLock() for key in self.vms}
        self.stopping = threading.Event()
        # Discovery and validation advertise exactly the configured images.
        self.tools = tools(backend.images)
        self.by_name = {t["name"]: t for t in self.tools}
        for vm in self.vms.values():
            if vm["state"] not in ("destroyed", "hibernated"):
                # Never guess whether a pre-crash command ran, or replay it.
                vm["state"] = "failed"
                self.store.save_vm(vm)
            if vm["state"] != "destroyed" and vm.get("image") not in backend.images:
                # Existing VMs keep private disks/kernels; only new VMs need the image.
                LOG.warning("vm=%s uses unconfigured image=%s", vm["id"], vm.get("image"))

    def public(self, vm):
        result = {
            k: vm[k]
            for k in (
                "id",
                "state",
                "network",
                "vcpus",
                "memory_mib",
                "idle_seconds",
                "idle_mode",
                "created_at",
                "last_activity",
                "image",
            )
        }
        # These handles permit reconciliation after an uncertain command response.
        result["jobs"] = vm["jobs"].copy()
        result["streams"] = len(self.streams.get(vm["id"], ()))
        return result

    def call(self, token, name, args):
        grant = self.store.authenticate(token)
        if name not in self.by_name:
            raise ServiceError("unknown_tool", "Unknown tool", 404)
        if not isinstance(args, dict):
            raise ServiceError("invalid_arguments", "Arguments must be an object")
        schema = self.by_name[name]["inputSchema"]
        errors = list(Draft202012Validator(schema).iter_errors(args))
        if errors:
            raise ServiceError("invalid_arguments", "Arguments do not match the tool schema")
        args = dict(args)
        if name == "vm_create":
            # The image ID is resolved in create, so an omitted image stays omitted.
            image = self.backend.images[args.get("image", self.backend.default_image)]
            args.setdefault("memory_mib", image.memory_mib)
        for key, prop in schema["properties"].items():
            if "default" in prop:
                args.setdefault(key, prop["default"])
        if name == "vm_create":
            with self.guard:
                return self.create(self.store.authenticate(token), args)
        if name == "vm_list":
            with self.guard:
                return {
                    "vms": [
                        self.public(v)
                        for v in self.vms.values()
                        if v["owner"] == grant["id"] and v["state"] != "destroyed"
                    ]
                }
        vm_id = args["vm_id"]
        with self.guard:
            vm = self.vms.get(vm_id)
            if vm is None or vm["owner"] != grant["id"]:
                raise ServiceError("not_found", "VM not found", 404)
            lock = self.locks[vm_id]
        with lock:
            # Recheck after waiting for a long operation, including token revocation.
            self.store.authenticate(token)
            result = self.operate(vm, name, args)
        LOG.info("operation=%s workspace=%s vm=%s", name, grant["id"], vm_id)
        return result

    def open_stream(self, token, vm_id, port):
        """Connect to a guest vsock port of an owned VM. The caller pumps the bytes."""
        grant = self.store.authenticate(token)
        with self.guard:
            vm = self.vms.get(vm_id)
            if vm is None or vm["owner"] != grant["id"]:
                raise ServiceError("not_found", "VM not found", 404)
            lock = self.locks[vm_id]
        allowed = any(low <= port <= high for low, high in self.stream_ports)
        if port in RESERVED_PORTS or not allowed:
            raise ServiceError("forbidden", "Guest port is not open to streams", 403)
        with lock:
            self.store.authenticate(token)
            if vm["state"] == "destroyed":
                raise ServiceError("not_found", "VM not found", 404)
            self.resume(vm)
            stream = Stream(vm_id, grant["id"], port)
            with self.guard:
                open_streams = [s for group in self.streams.values() for s in group]
                if (
                    len(open_streams) >= self.max_streams
                    or sum(s.owner == grant["id"] for s in open_streams)
                    >= self.max_streams_per_workspace
                    or len(self.streams.get(vm_id, ())) >= self.max_streams_per_vm
                ):
                    raise ServiceError("quota_exceeded", "Stream limit reached", 409)
                self.streams.setdefault(vm_id, set()).add(stream)
            try:
                stream.sock = self.backend.connect(vm, port)
            except BaseException:
                with self.guard:
                    self.streams[vm_id].discard(stream)
                raise
            vm["last_activity"] = time.time()
            self.store.save_vm(vm)
        LOG.info("stream=open workspace=%s vm=%s port=%d", grant["id"], vm_id, port)
        return stream

    def close_stream(self, stream, reason="closed"):
        stream.close(reason)
        with self.guard:
            self.streams.get(stream.vm_id, set()).discard(stream)
        if stream.sock is not None:
            stream.sock.close()
        # Metadata only; stream payload is never logged.
        LOG.info(
            "stream=close workspace=%s vm=%s port=%d reason=%s seconds=%.1f "
            "bytes_to_guest=%d bytes_from_guest=%d",
            stream.owner,
            stream.vm_id,
            stream.port,
            stream.reason,
            time.monotonic() - stream.opened,
            stream.to_guest,
            stream.from_guest,
        )

    def close_streams(self, vm, reason):
        # Called under the VM lock. Pumps notice the shut-down socket and finish.
        with self.guard:
            streams = self.streams.pop(vm["id"], set())
        for stream in streams:
            stream.close(reason)

    def pipes(self, vm):
        with self.guard:
            return list(self.brokers.get(vm["id"], {}).values())

    def broker_attached(self, vm):
        return any(pipe.session is not None for pipe in self.pipes(vm))

    def ensure_broker_listener(self, vm):
        current = self.broker_listeners.get(vm["id"])
        if current is not None and current.alive():
            return
        if current is not None:
            current.close()
        sock = self.backend.broker_listen(vm)
        self.broker_listeners[vm["id"]] = BrokerAcceptor(
            sock,
            lambda job_id, route, conn, vm_id=vm["id"]: self.route_broker(
                vm_id, job_id, route, conn
            ),
        )

    def close_broker_listener(self, vm):
        listener = self.broker_listeners.pop(vm["id"], None)
        if listener is not None:
            listener.close()

    def route_broker(self, vm_id, job_id, route, sock):
        """Acceptor thread. Any guest process can reach port 1023; only the job's
        listener knows its route secret, so the job id alone never selects a pipe."""
        with self.guard:
            pipe = self.brokers.get(vm_id, {}).get(job_id)
        expected = pipe.route if pipe is not None else b"\0" * len(route)
        if not hmac.compare_digest(expected, route) or pipe is None:
            sock.close()
            return
        pipe.add_guest(sock)

    def register_broker(self, vm, job_id, token):
        """Return the route secret for the guest listener. It is not the token."""
        route = secrets.token_hex(32)
        pipe = BrokerPipe(
            vm["id"],
            vm["owner"],
            job_id,
            hashlib.sha256(token.encode()).digest(),
            route.encode(),
        )
        with self.guard:
            self.brokers.setdefault(vm["id"], {})[job_id] = pipe
        return route

    def retire_broker(self, vm, job_id, reason):
        with self.guard:
            pipes = self.brokers.get(vm["id"], {})
            pipe = pipes.pop(job_id, None)
            if not pipes:
                self.brokers.pop(vm["id"], None)
        if pipe is None:
            return
        pipe.retire(reason)
        if not self.pipes(vm):
            self.close_broker_listener(vm)

    def retire_finished_brokers(self, vm):
        for pipe in self.pipes(vm):
            if vm["jobs"].get(pipe.job_id, {}).get("state") == "completed":
                self.retire_broker(vm, pipe.job_id, "job finished")

    def detach_brokers(self, vm, reason):
        for pipe in self.pipes(vm):
            pipe.detach(reason)

    def forget_brokers(self, vm):
        with self.guard:
            pipes = self.brokers.pop(vm["id"], {})
        for pipe in pipes.values():
            pipe.retire("destroyed")
        self.close_broker_listener(vm)

    def open_broker(self, credential, vm_id, job_id, pipe_token, loop):
        """Attach the controller that presented this job's one-time token."""
        grant = self.store.authenticate(credential)
        if not isinstance(pipe_token, str) or not 43 <= len(pipe_token) <= 128:
            raise ServiceError("not_found", "Job not found", 404)
        with self.guard:
            vm = self.vms.get(vm_id)
            if vm is None or vm["owner"] != grant["id"]:
                raise ServiceError("not_found", "VM not found", 404)
            lock = self.locks[vm_id]
        with lock:
            self.store.authenticate(credential)
            with self.guard:
                pipe = self.brokers.get(vm_id, {}).get(job_id)
            offered = hashlib.sha256(pipe_token.encode()).digest()
            expected = pipe.token_hash if pipe is not None else b"\0" * 32
            # Same status for an unknown job, another workspace's VM, and a wrong token.
            if (
                not hmac.compare_digest(expected, offered)
                or pipe is None
                or pipe.retired is not None
                or vm["state"] == "destroyed"
                or vm["jobs"].get(job_id, {}).get("state") == "completed"
            ):
                raise ServiceError("not_found", "Job not found", 404)
            if sum(p.session is not None for p in self.pipes(vm)) >= MAX_ATTACHED:
                raise ServiceError("quota_exceeded", "Broker pipe limit reached", 409)
            self.resume(vm)
            self.ensure_broker_listener(vm)
            session = pipe.attach(loop, self.stream_rate_bytes)
            vm["last_activity"] = time.time()
            self.store.save_vm(vm)
        # The token and the pipe payload are not logged.
        LOG.info("broker=open workspace=%s vm=%s job=%s", grant["id"], vm_id, job_id)
        return session

    def close_broker(self, session, reason):
        """End one attachment. A late call for an older attachment leaves a newer one."""
        pipe = session.pipe
        pipe.detach(reason, session)
        if session.logged:
            return
        session.logged = True
        LOG.info(
            "broker=close workspace=%s vm=%s job=%s reason=%s seconds=%.1f "
            "bytes_to_guest=%d bytes_from_guest=%d",
            pipe.owner,
            pipe.vm_id,
            pipe.job_id,
            session.reason,
            time.monotonic() - session.opened_at,
            session.to_guest,
            session.from_guest,
        )

    def create(self, grant, args):
        old = self.store.reserve(grant["id"], "vm_create", args["request_id"], args)
        if old is not None:
            if "error" in old:
                raise ServiceError(**old["error"])
            return old
        try:
            image = self.backend.images[args.get("image", self.backend.default_image)]
            if grant["images"] is not None and image.id not in grant["images"]:
                raise ServiceError("forbidden", "Workspace cannot use this image", 403)
            if args["memory_mib"] < image.min_memory_mib:
                raise ServiceError(
                    "invalid_arguments", f"Image requires memory_mib >= {image.min_memory_mib}"
                )
            live = [v for v in self.vms.values() if v["state"] != "destroyed"]
            owned = [v for v in live if v["owner"] == grant["id"]]
            if len(live) >= self.max_vms or len(owned) >= grant["max_vms"]:
                raise ServiceError("quota_exceeded", "VM count quota exceeded", 409)
            if args["memory_mib"] > grant["max_memory_mib"] or args["vcpus"] > grant["max_vcpus"]:
                raise ServiceError("quota_exceeded", "VM resource limit exceeded", 403)
            if sum(v["memory_mib"] for v in live) + args["memory_mib"] > self.max_memory_mib:
                raise ServiceError("quota_exceeded", "Host VM memory reservation exceeded", 409)
            if hasattr(self.backend, "storage_reservation"):
                candidate = {**args, "image": image.id}
                reserved = sum(self.backend.storage_reservation(v) for v in [*live, candidate])
                if reserved > self.max_storage_bytes:
                    raise ServiceError("quota_exceeded", "Host disk reservation exceeded", 409)
            if args["network"] == "on" and not grant["network"]:
                raise ServiceError("forbidden", "Workspace cannot enable networking", 403)
            used = {v["slot"] for v in live}
            slot = next(i for i in range(self.max_vms) if i not in used)
            now = time.time()
            vm = {
                "id": uuid.uuid4().hex,
                "owner": grant["id"],
                "slot": slot,
                "state": "creating",
                "created_at": now,
                "last_activity": now,
                "runtime_id": self.backend.runtime_id,
                "jobs": {},
                **args,
                "image": image.id,
            }
            self.vms[vm["id"]] = vm
            self.locks[vm["id"]] = threading.RLock()
            self.store.save_vm(vm)
            try:
                self.backend.create(vm)
                vm["state"] = "running"
            except Exception:
                vm["state"] = "failed"
                raise
            finally:
                self.store.save_vm(vm)
            result = self.public(vm)
            self.store.finish(grant["id"], "vm_create", args["request_id"], result)
            return result
        except ServiceError as exc:
            self.store.finish(
                grant["id"],
                "vm_create",
                args["request_id"],
                {
                    "error": {"code": exc.code, "message": str(exc), "status": exc.status},
                },
            )
            raise

    def resume(self, vm):
        if vm["state"] in ("paused", "hibernated"):
            # The checkpoint needs its own VMM; its kernel and disk are already private.
            if vm.get("runtime_id", self.backend.runtime_id) != self.backend.runtime_id:
                raise ServiceError(
                    "incompatible_runtime", "Restore requires the original VMM binary", 409
                )
            previous = vm["state"]
            vm["state"] = "resuming"
            self.store.save_vm(vm)
            try:
                self.backend.resume(vm, previous)
                vm["state"] = "running"
            except Exception:
                vm["state"] = "failed"
                raise
            finally:
                self.store.save_vm(vm)
        if vm["state"] != "running":
            raise ServiceError("invalid_state", "VM is not runnable", 409)
        # Hibernate relaunch removes the vsock socket; pause keeps the listener.
        if self.pipes(vm):
            self.ensure_broker_listener(vm)

    def refresh(self, vm):
        status = self.backend.guest(vm, "status", {})
        # Guest reports affect scheduling only, never ownership or host paths.
        for job_id in vm["jobs"]:
            if job_id in status["jobs"]:
                vm["jobs"][job_id] = status["jobs"][job_id]
        active = bool(status["active"])
        if active:
            vm["last_activity"] = time.time()
        self.store.save_vm(vm)
        self.retire_finished_brokers(vm)
        return active

    def suspend(self, vm, mode, force=False):
        target = "paused" if mode == "pause" else "hibernated"
        if vm["state"] == target:
            return self.public(vm)
        if vm["state"] == "hibernated" and mode == "pause":
            raise ServiceError("invalid_state", "VM is already hibernated", 409)
        if vm["state"] not in ("running", "paused"):
            raise ServiceError("invalid_state", "VM cannot be suspended", 409)
        if vm["state"] == "running" and not force:
            if self.streams.get(vm["id"]):
                raise ServiceError("busy", "Open streams prevent suspension", 409)
            if self.broker_attached(vm):
                raise ServiceError("busy", "Open broker pipe prevents suspension", 409)
            if self.refresh(vm):
                raise ServiceError("busy", "Active jobs prevent suspension", 409)
        # Firecracker resets vsock connections across a restore; clients reconnect.
        self.close_streams(vm, "suspended")
        self.detach_brokers(vm, "suspended")
        if mode != "pause":
            self.close_broker_listener(vm)
        previous = vm["state"]
        vm["state"] = "suspending"
        self.store.save_vm(vm)
        try:
            self.backend.suspend(vm, mode, previous)
            vm["state"] = target
        except Exception:
            self.close_broker_listener(vm)
            vm["state"] = "failed"
            raise
        finally:
            self.store.save_vm(vm)
        return self.public(vm)

    def operate(self, vm, name, args):
        if name == "vm_status":
            return self.public(vm)
        if name == "vm_destroy":
            if vm["state"] != "destroyed":
                self.close_streams(vm, "destroyed")
                self.forget_brokers(vm)
                self.backend.destroy(vm)
                vm["state"] = "destroyed"
                self.store.save_vm(vm)
            return self.public(vm)
        if vm["state"] == "destroyed":
            raise ServiceError("not_found", "VM not found", 404)
        if name == "vm_suspend":
            return self.suspend(vm, args["mode"], args["force"])
        if "job_id" in args and args["job_id"] not in vm["jobs"]:
            raise ServiceError("not_found", "Job not found", 404)
        if name == "vm_job_status":
            if vm["state"] == "running":
                self.refresh(vm)
            return {"job_id": args["job_id"], "vm_state": vm["state"], **vm["jobs"][args["job_id"]]}
        self.resume(vm)
        vm["last_activity"] = time.time()
        self.store.save_vm(vm)
        if name == "vm_resume":
            return self.public(vm)
        if name == "vm_exec":
            # The token is not part of the retry receipt, the guest RPC, or the result.
            pipe_token = args.pop("broker_pipe_token", None)
            operation = f"vm_exec:{vm['id']}"
            old = self.store.reserve(vm["owner"], operation, args["request_id"], args)
            if old is not None:
                return old
            if len(vm["jobs"]) >= 256:
                raise ServiceError("quota_exceeded", "Maximum 256 jobs per VM", 409)
            job_id = uuid.uuid4().hex
            # Persist the handle before dispatch. Transport failure leaves a discoverable
            # uncertain job; never automatically replay an effectful command.
            vm["jobs"][job_id] = {"state": "unknown"}
            self.store.save_vm(vm)
            guest_args = {**args, "job_id": job_id}
            if pipe_token is not None:
                self.ensure_broker_listener(vm)
                guest_args["broker_pipe"] = True
                guest_args["broker_route"] = self.register_broker(vm, job_id, pipe_token)
            try:
                result = self.backend.guest(vm, "exec", guest_args)
            except BaseException:
                if pipe_token is not None:
                    self.retire_broker(vm, job_id, "exec failed")
                raise
            if pipe_token is not None:
                # Images built before the pipe ignore it; report what the guest did.
                supported = result.get("broker_pipe") is True
                if not supported:
                    self.retire_broker(vm, job_id, "unsupported guest")
                result = {"job_id": result["job_id"], "broker_pipe": supported}
            vm["jobs"][job_id] = {"state": "running"}
            self.store.save_vm(vm)
            self.store.finish(vm["owner"], operation, args["request_id"], result)
            return result
        result = self.backend.guest(vm, name.removeprefix("vm_"), args)
        if "job_id" in args and result.get("state") == "completed":
            self.retire_broker(vm, args["job_id"], "job finished")
        return result

    def sweep(self):
        with self.guard:
            entries = [(v, self.locks[v["id"]]) for v in self.vms.values()]
        for vm, lock in entries:
            if not lock.acquire(blocking=False):
                continue
            try:
                if vm["state"] != "running":
                    continue
                now = time.monotonic()
                if self.pipes(vm) and now - self.broker_checked.get(vm["id"], 0) >= (
                    BROKER_RECHECK_SECONDS
                ):
                    # Retires pipes of finished jobs, so they stop counting as activity.
                    self.broker_checked[vm["id"]] = now
                    self.refresh(vm)
                if not vm["idle_seconds"]:
                    continue
                if self.streams.get(vm["id"]) or self.broker_attached(vm):
                    # An attached client is activity; idle time counts from its close.
                    vm["last_activity"] = time.time()
                    continue
                if self.refresh(vm):
                    continue
                if time.time() - vm["last_activity"] >= vm["idle_seconds"]:
                    self.suspend(vm, vm["idle_mode"])
            except Exception:
                LOG.exception("idle suspension failed for vm=%s", vm["id"])
            finally:
                lock.release()

    def watch(self):
        while not self.stopping.wait(1):
            self.sweep()

    def close(self):
        self.stopping.set()
        # Normal shutdown preserves runnable guests with a checkpoint. Failure is
        # visible and the process is stopped; no stale pre-command snapshot is reused.
        for vm in self.vms.values():
            with self.locks[vm["id"]]:
                if vm["state"] in ("running", "paused"):
                    try:
                        self.suspend(vm, "hibernate", force=True)
                    except Exception:
                        LOG.exception("shutdown checkpoint failed for vm=%s", vm["id"])
                        self.backend.stop(vm)
