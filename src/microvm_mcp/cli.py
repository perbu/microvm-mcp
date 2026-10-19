import argparse
import fcntl
import logging
import os
import sys
import tomllib
from pathlib import Path

from . import images
from .adapter import manifest
from .catalog import tools
from .store import Store


def stream_settings(config):
    keys = (
        "stream_ports",
        "max_streams",
        "max_streams_per_workspace",
        "max_streams_per_vm",
        "stream_rate_bytes",
    )
    return {key: config[key] for key in keys if key in config}


def main():
    parser = argparse.ArgumentParser(description="Standalone development microVM service")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--config", type=Path, required=True)
    issue = sub.add_parser("issue-token", help="Local administrator operation; writes a 0600 file")
    issue.add_argument("--state-dir", required=True)
    issue.add_argument("--workspace", required=True)
    issue.add_argument("--output", type=Path, required=True)
    issue.add_argument("--ttl", type=int, default=86400)
    issue.add_argument("--max-vms", type=int, default=1)
    issue.add_argument("--max-memory-mib", type=int, default=2048)
    issue.add_argument("--max-vcpus", type=int, default=2)
    issue.add_argument("--allow-network", action="store_true")
    issue.add_argument(
        "--image",
        action="append",
        dest="images",
        help="Restrict the workspace to this image ID (repeatable); default: any",
    )
    attach = sub.add_parser(
        "attach", help="Bridge stdin/stdout to a guest vsock port of an owned VM"
    )
    attach.add_argument("--endpoint", default="http://127.0.0.1:8090")
    attach.add_argument("--token-file", type=Path, required=True)
    attach.add_argument("--vm", required=True)
    attach.add_argument("--port", type=int, required=True)
    attach.add_argument(
        "--ignore-stdin-eof",
        action="store_true",
        help="Do not send EOF when stdin ends; wait for the guest to close (downloads)",
    )
    revoke = sub.add_parser(
        "revoke-workspace", help="Block future calls; does not stop running jobs"
    )
    revoke.add_argument("--state-dir", required=True)
    revoke.add_argument("--workspace", required=True)
    adapter = sub.add_parser("demo-manifest")
    adapter.add_argument("--endpoint", default="http://127.0.0.1:8090")
    adapter.add_argument("--secret-ref", default="microvm_workspace")
    adapter.add_argument("--vm-class", default="exec")
    adapter.add_argument("--config", type=Path, help="List this service's image IDs")
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "demo-manifest":
        catalog = tools()
        if args.config:
            catalog = tools(images.load(tomllib.loads(args.config.read_text()))[0])
        print(manifest(args.endpoint, args.secret_ref, args.vm_class, catalog), end="")
    elif args.command == "issue-token":
        # O_EXCL avoids accidentally overwriting the sole copy of a live token.
        with args.output.open("x") as output:
            os.chmod(args.output, 0o600)
            token = Store(args.state_dir).issue(
                args.workspace,
                ttl=args.ttl,
                max_vms=args.max_vms,
                max_memory_mib=args.max_memory_mib,
                max_vcpus=args.max_vcpus,
                network=args.allow_network,
                images=args.images,
            )
            output.write(token + "\n")
        print(f"Credential written to {args.output}; keep it on the trusted host.")
    elif args.command == "attach":
        from .attach import attach

        token = args.token_file.read_text().strip()
        sys.exit(
            attach(args.endpoint, token, args.vm, args.port, send_eof=not args.ignore_stdin_eof)
        )
    elif args.command == "revoke-workspace":
        Store(args.state_dir).revoke(args.workspace)
    else:
        import uvicorn

        from .firecracker import Firecracker
        from .server import MAX_FRAME, create_app
        from .service import Service

        logging.basicConfig(level=logging.INFO)
        config = tomllib.loads(args.config.read_text())
        store = Store(config["state_dir"])
        with (store.directory / "service.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            backend = Firecracker(config)
            backend.preflight()
            service = Service(
                store,
                backend,
                max_vms=config.get("max_vms", 16),
                max_memory_mib=config.get("max_memory_mib", 16384),
                max_storage_bytes=config.get("max_storage_gib", 64) * 1024**3,
                **stream_settings(config),
            )
            # Clean up leftover live VMMs by cgroup before accepting requests.
            for vm in service.vms.values():
                if vm["state"] == "failed":
                    backend.stop(vm)
                    backend.network.destroy(vm)
            port = config.get("port", 8090)
            hosts = config.get("allowed_hosts", [f"127.0.0.1:{port}", f"localhost:{port}"])
            # Remote access goes through a TLS reverse proxy. Never bind cleartext remotely.
            uvicorn.run(
                create_app(service, hosts),
                host="127.0.0.1",
                port=port,
                # Open streams and broker pipes hold connections; keep 64 for requests.
                limit_concurrency=64 + service.max_streams + service.max_vms * 4,
                ws="websockets-sansio",
                ws_max_size=MAX_FRAME,
                timeout_keep_alive=5,
                access_log=False,
                proxy_headers=False,
            )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        print(f"microvm-mcp: {exc}", file=sys.stderr)
        sys.exit(1)
