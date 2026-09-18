import logging
import threading
import time
import uuid

from jsonschema import Draft202012Validator

from .catalog import BY_NAME
from .errors import ServiceError

LOG = logging.getLogger(__name__)


class Service:
    def __init__(
        self, store, backend, *, max_vms=16, max_memory_mib=16384, max_storage_bytes=64 * 1024**3
    ):
        if not 1 <= max_vms <= 64:
            raise ValueError("max_vms must be between 1 and 64")
        self.store, self.backend, self.max_vms = store, backend, max_vms
        self.max_memory_mib = max_memory_mib
        self.max_storage_bytes = max_storage_bytes
        self.guard = threading.RLock()
        self.vms = {v["id"]: v for v in store.vms()}
        self.locks = {key: threading.RLock() for key in self.vms}
        self.stopping = threading.Event()
        for vm in self.vms.values():
            if vm["state"] not in ("destroyed", "hibernated"):
                # Never guess whether a pre-crash command ran, or replay it.
                vm["state"] = "failed"
                self.store.save_vm(vm)

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
        return result

    def call(self, token, name, args):
        grant = self.store.authenticate(token)
        if name not in BY_NAME:
            raise ServiceError("unknown_tool", "Unknown tool", 404)
        if not isinstance(args, dict):
            raise ServiceError("invalid_arguments", "Arguments must be an object")
        errors = list(Draft202012Validator(BY_NAME[name]["inputSchema"]).iter_errors(args))
        if errors:
            raise ServiceError("invalid_arguments", "Arguments do not match the tool schema")
        args = dict(args)
        for k, schema in BY_NAME[name]["inputSchema"]["properties"].items():
            if "default" in schema:
                args.setdefault(k, schema["default"])
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

    def create(self, grant, args):
        old = self.store.reserve(grant["id"], "vm_create", args["request_id"], args)
        if old is not None:
            if "error" in old:
                raise ServiceError(**old["error"])
            return old
        try:
            live = [v for v in self.vms.values() if v["state"] != "destroyed"]
            owned = [v for v in live if v["owner"] == grant["id"]]
            if len(live) >= self.max_vms or len(owned) >= grant["max_vms"]:
                raise ServiceError("quota_exceeded", "VM count quota exceeded", 409)
            if args["memory_mib"] > grant["max_memory_mib"] or args["vcpus"] > grant["max_vcpus"]:
                raise ServiceError("quota_exceeded", "VM resource limit exceeded", 403)
            if sum(v["memory_mib"] for v in live) + args["memory_mib"] > self.max_memory_mib:
                raise ServiceError("quota_exceeded", "Host VM memory reservation exceeded", 409)
            if hasattr(self.backend, "storage_reservation"):
                reserved = sum(self.backend.storage_reservation(v) for v in [*live, args])
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
                "image": self.backend.image_id,
                "runtime_id": getattr(self.backend, "runtime_id", self.backend.image_id),
                "jobs": {},
                **args,
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
            runtime_id = getattr(self.backend, "runtime_id", self.backend.image_id)
            if vm.get("runtime_id", runtime_id) != runtime_id:
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
        return active

    def suspend(self, vm, mode, force=False):
        target = "paused" if mode == "pause" else "hibernated"
        if vm["state"] == target:
            return self.public(vm)
        if vm["state"] == "hibernated" and mode == "pause":
            raise ServiceError("invalid_state", "VM is already hibernated", 409)
        if vm["state"] not in ("running", "paused"):
            raise ServiceError("invalid_state", "VM cannot be suspended", 409)
        if vm["state"] == "running" and not force and self.refresh(vm):
            raise ServiceError("busy", "Active jobs prevent suspension", 409)
        previous = vm["state"]
        vm["state"] = "suspending"
        self.store.save_vm(vm)
        try:
            self.backend.suspend(vm, mode, previous)
            vm["state"] = target
        except Exception:
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
            result = self.backend.guest(vm, "exec", {**args, "job_id": job_id})
            vm["jobs"][job_id] = {"state": "running"}
            self.store.save_vm(vm)
            self.store.finish(vm["owner"], operation, args["request_id"], result)
            return result
        return self.backend.guest(vm, name.removeprefix("vm_"), args)

    def sweep(self):
        with self.guard:
            entries = [(v, self.locks[v["id"]]) for v in self.vms.values()]
        for vm, lock in entries:
            if not lock.acquire(blocking=False):
                continue
            try:
                if vm["state"] != "running" or not vm["idle_seconds"]:
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
