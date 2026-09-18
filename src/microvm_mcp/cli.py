import argparse
import fcntl
import logging
import os
import sys
import tomllib
from pathlib import Path

from .adapter import manifest
from .store import Store


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
    revoke = sub.add_parser(
        "revoke-workspace", help="Block future calls; does not stop running jobs"
    )
    revoke.add_argument("--state-dir", required=True)
    revoke.add_argument("--workspace", required=True)
    adapter = sub.add_parser("demo-manifest")
    adapter.add_argument("--endpoint", default="http://127.0.0.1:8090")
    adapter.add_argument("--secret-ref", default="microvm_workspace")
    adapter.add_argument("--vm-class", default="exec")
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "demo-manifest":
        print(manifest(args.endpoint, args.secret_ref, args.vm_class), end="")
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
            )
            output.write(token + "\n")
        print(f"Credential written to {args.output}; keep it on the trusted host.")
    elif args.command == "revoke-workspace":
        Store(args.state_dir).revoke(args.workspace)
    else:
        import uvicorn

        from .firecracker import Firecracker
        from .server import create_app
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
                limit_concurrency=64,
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
