#!/usr/bin/python3
"""Runs ONLY inside a full Linux guest. Never import as a host execution backend."""

import base64
import json
import os
import select
import selectors
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

WORKSPACE = Path("/workspace")
CGROUP = Path("/sys/fs/cgroup/jobs")
OUTPUT_LIMIT = 256 * 1024
FILE_LIMIT = 128 * 1024
JOBS = {}
MANAGED_PIDS = set()
LOCK = threading.RLock()


def workspace_path(path):
    parts = Path(path).parts
    if Path(path).is_absolute() or ".." in parts:
        raise ValueError("path must be relative to workspace")
    return parts


def open_workspace(path, flags, mode=0o644):
    parts = workspace_path(path)
    if not parts:
        raise ValueError("file path required")
    parent = os.open(WORKSPACE, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = new
        fd = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=parent)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("regular files only")
        return fd
    finally:
        os.close(parent)


def populated(group):
    return "populated 1" in (group / "cgroup.events").read_text()


def execute_child(group, cwd, command, ready_fd):
    # Enter the cgroup before dropping privileges and before customer code executes.
    (Path(group) / "cgroup.procs").write_text(str(os.getpid()))
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)
    os.chdir(cwd)
    os.write(int(ready_fd), b"1")
    os.close(int(ready_fd))
    os.execve(
        "/bin/bash",
        ["bash", "-lc", command],
        {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/home/developer",
            "LANG": "C.UTF-8",
            "USER": "developer",
        },
    )


def supervise(job, process, group, timeout):
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    os.set_blocking(process.stdout.fileno(), False)
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        while True:
            if time.monotonic() >= deadline:
                (group / "cgroup.kill").write_text("1")
                job["timed_out"] = True
            for key, _ in selector.select(0.1):
                data = os.read(key.fd, 16384)
                if not data:
                    selector.unregister(key.fileobj)
                else:
                    with LOCK:
                        remaining = OUTPUT_LIMIT - len(job["output"])
                        job["output"].extend(data[:remaining])
                        job["truncated"] |= len(data) > remaining
            process.poll()  # reap the main child; descendants still count as activity
            if not populated(group) and not selector.get_map():
                break
        with LOCK:
            job["exit_code"] = process.wait()
            job["state"] = "completed"
            MANAGED_PIDS.discard(process.pid)
    finally:
        selector.close()
        process.stdout.close()


def summary(job):
    return {k: job[k] for k in ("state", "exit_code", "truncated", "timed_out")}


def handle(op, args):
    with LOCK:
        if op == "status":
            return {
                "active": any(j["state"] == "running" for j in JOBS.values()),
                "jobs": {key: summary(j) for key, j in JOBS.items()},
            }
        if op == "exec":
            job_id = args["job_id"]
            if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
                raise ValueError("bad job id")
            if job_id in JOBS:
                return {"job_id": job_id}
            if len(JOBS) >= 256 or sum(j["state"] == "running" for j in JOBS.values()) >= 4:
                raise ValueError("job quota exceeded")
            parts = workspace_path(args.get("cwd", "."))
            cwd = WORKSPACE.joinpath(*parts).resolve(strict=True)
            if not cwd.is_relative_to(WORKSPACE):
                raise ValueError("working directory escapes workspace")
            group = CGROUP / job_id
            group.mkdir()
            (group / "pids.max").write_text("128")
            job = {
                "state": "running",
                "exit_code": None,
                "truncated": False,
                "timed_out": False,
                "output": bytearray(),
                "group": group,
            }
            ready_read, ready_write = os.pipe()
            try:
                process = subprocess.Popen(
                    [
                        "/usr/bin/python3",
                        __file__,
                        "--job-child",
                        str(group),
                        str(cwd),
                        args["command"],
                        str(ready_write),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                    pass_fds=(ready_write,),
                )
                os.close(ready_write)
                ready_write = -1
                if not select.select([ready_read], [], [], 2)[0] or os.read(ready_read, 1) != b"1":
                    process.kill()
                    (group / "cgroup.kill").write_text("1")
                    process.wait()
                    process.stdout.close()
                    raise ValueError("job could not enter its cgroup")
            finally:
                os.close(ready_read)
                if ready_write != -1:
                    os.close(ready_write)
            MANAGED_PIDS.add(process.pid)
            JOBS[job_id] = job
            threading.Thread(
                target=supervise, args=(job, process, group, args["timeout_seconds"]), daemon=True
            ).start()
            return {"job_id": job_id}
        if op in ("job_status", "job_output", "job_cancel"):
            job = JOBS[args["job_id"]]
            if op == "job_cancel" and job["state"] == "running":
                (job["group"] / "cgroup.kill").write_text("1")
            result = {"job_id": args["job_id"], **summary(job)}
            if op == "job_output":
                cursor = args.get("cursor", 0)
                if cursor > len(job["output"]):
                    raise ValueError("cursor beyond retained output")
                chunk = job["output"][cursor : cursor + 32768]
                result.update(
                    data_base64=base64.b64encode(chunk).decode(), next_cursor=cursor + len(chunk)
                )
            return result
        if op == "read_file":
            with os.fdopen(open_workspace(args["path"], os.O_RDONLY), "rb") as src:
                data = src.read(FILE_LIMIT + 1)
            if len(data) > FILE_LIMIT:
                raise ValueError("file too large")
            return {"data_base64": base64.b64encode(data).decode()}
        if op == "write_file":
            data = base64.b64decode(args["data_base64"], validate=True)
            if len(data) > FILE_LIMIT:
                raise ValueError("file too large")
            # O_TRUNC only after fstat: opening a guest device must never have effects.
            fd = open_workspace(args["path"], os.O_WRONLY | os.O_CREAT)
            with os.fdopen(fd, "wb") as dst:
                os.ftruncate(dst.fileno(), 0)
                os.fchown(dst.fileno(), 1000, 1000)
                dst.write(data)
            return {"bytes_written": len(data)}
        raise ValueError("unknown operation")


def reap_orphans():
    # PID 1 adopts detached descendants. Do not steal Popen's main-child status.
    while True:
        with LOCK:
            for children in Path("/proc/self/task").glob("*/children"):
                try:
                    pids = children.read_text().split()
                except FileNotFoundError:
                    continue
                for value in pids:
                    pid = int(value)
                    if pid not in MANAGED_PIDS:
                        try:
                            os.waitpid(pid, os.WNOHANG)
                        except ChildProcessError:
                            pass
        time.sleep(0.1)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--job-child":
        execute_child(*sys.argv[2:])
        return
    CGROUP.mkdir(exist_ok=True)
    (CGROUP / "cgroup.subtree_control").write_text("+pids")
    threading.Thread(target=reap_orphans, daemon=True).start()
    listener = socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)
    listener.bind((socket.VMADDR_CID_ANY, 1024))
    listener.listen(16)
    print("MICROVM_READY", flush=True)
    while True:
        # Sequential bounded RPCs. Commands themselves run asynchronously.
        connection, peer = listener.accept()
        with connection:
            if peer[0] != socket.VMADDR_CID_HOST:
                continue
            connection.settimeout(10)
            with connection.makefile("rwb") as file:
                try:
                    raw = file.readline(512 * 1024 + 1)
                    if len(raw) > 512 * 1024 or not raw.endswith(b"\n"):
                        raise ValueError("oversize request")
                    request = json.loads(raw)
                    result = handle(request["op"], request["args"])
                except Exception:
                    result = {"error": "operation failed"}
                try:
                    file.write(json.dumps(result).encode() + b"\n")
                    file.flush()
                except OSError:
                    pass


if __name__ == "__main__":
    main()
