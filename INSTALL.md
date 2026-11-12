# Installing from this tree as a systemd service

A start-to-finish runbook for turning the current working tree into a running,
boot-enabled service on a single KVM host. It uses the Debian packages and
the packaged systemd unit. For background and requirements see
[README.md](README.md) and the [Debian packaging guide](packaging/debian/README.md).

The examples use port **1050**, the state directory `/large/microvm-mcp` (a
reflink-capable XFS volume) and image ID `debian-13-dev-v3`. Substitute your own.

## 1. Check, then build packages

```sh
uv run --locked ruff check src guest tests scripts examples
uv run --locked ruff format --check src guest tests scripts examples
uv run --locked pytest
scripts/build-debs            # writes dist/debian/*.deb and SHA256SUMS
```

`build-debs` packages the **working tree**, including uncommitted changes.
Lintian errors make it fail *after* `dpkg-buildpackage` succeeds, and then
nothing is copied to `dist/debian/`. A new Python dependency with a native
extension needs an `unstripped-binary-or-object` line in
`debian/microvm-mcp.lintian-overrides`.

## 2. Install or upgrade the packages

```sh
sudo systemctl stop microvm-mcp.service      # skip on a first install
cd dist/debian
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --reinstall \
  -o Dpkg::Options::=--force-confold \
  ./microvm-mcp_*.deb ./microvm-mcp-python_*.deb ./microvm-mcp-firecracker-1.17_*.deb
```

- `--reinstall` is needed while `debian/changelog` still has the same version
  as the installed package; otherwise APT reports "already the newest version"
  and installs nothing. Bump the changelog for anything you distribute.
- `--force-confold` keeps your edited `/etc/microvm-mcp.toml` instead of
  stopping at dpkg's interactive conffile prompt.
- The packages never enable or start the service, and an upgrade leaves it stopped.

## 3. Build and install a guest image (only when `guest/` changed)

Running VMs and checkpoints keep their old supervisor, and so does an
already-installed image. After any change under `guest/`, build a new image
under a **new** ID:

```sh
sudo scripts/build-image /large/microvm-image-build/v3
sudo cat /large/microvm-image-build/v3/debian-13.ext4.sha256

sudo microvm-mcp-install-image --state-dir /large/microvm-mcp \
  --image-id debian-13-dev-v3 \
  --rootfs /large/microvm-image-build/v3/debian-13.ext4 --rootfs-sha256 <rootfs digest> \
  --kernel <pinned vmlinux> --kernel-sha256 <kernel digest>
```

The kernel can be the previous bundle's `vmlinux`
(`/large/microvm-mcp/images/<old-id>/vmlinux`) with its recorded digest. The
installer prints an `[images.<id>]` table for step 4. To check that a built image
has the current supervisor:

```sh
sudo debugfs -R "cat /opt/microvm/supervisor.py" <rootfs.ext4> | sha256sum
sha256sum guest/supervisor.py
```

## 4. Configure `/etc/microvm-mcp.toml`

The listener binds `127.0.0.1` unless `listen` says otherwise. This host binds
every IPv4 and IPv6 interface. **Changing `port` or `listen` also requires
changing `allowed_hosts`**: requests whose `Host` header is not listed are
refused, so list every name and address clients use, with IPv6 literals
bracketed. A stale list breaks every client after a port change.

```toml
port = 1050
listen = ["0.0.0.0", "::"]     # cleartext HTTP on all interfaces
allowed_hosts = [
  "127.0.0.1:1050", "localhost:1050", "[::1]:1050",
  "hal:1050", "<public IPv4>:1050", "[<public IPv6>]:1050",
]

state_dir = "/large/microvm-mcp"
firecracker = "/usr/lib/microvm-mcp/firecracker/1.17.0/firecracker"
jailer = "/usr/lib/microvm-mcp/firecracker/1.17.0/jailer"

uid_base = 62000          # UIDs 62000..62000+max_vms-1 reserved for jails
max_vms = 16
max_memory_mib = 16384
max_storage_gib = 64

uplink = "enp14s0"                  # only for network=on; ip_forward must be 1
blocked_cidrs = ["<host public subnet>/26"]

# Optional, for network="packages" (see README, "Package-only networking").
# [packages]
# pypi = "http://<mirror IPv4>:3141/root/pypi/+simple/"
# npm = "http://<mirror IPv4>:4873/"

[images.debian-13-dev-v3]
rootfs_sha256 = "<rootfs digest>"
kernel_sha256 = "<kernel digest>"
default = true
```

Do not mix the legacy top-level `image_id`/`rootfs`/`kernel`/`*_sha256` keys
with `[images.*]` tables; that is a startup error. Keep a copy of the previous
file (for example `/etc/microvm-mcp.toml.pre-<change>`) before editing.

## 5. Order the unit after the state volume, enable at boot

When `state_dir` is on its own mount, add a drop-in so that systemd does not
start the service before the volume is mounted:

```sh
sudo mkdir -p /etc/systemd/system/microvm-mcp.service.d
sudo tee /etc/systemd/system/microvm-mcp.service.d/host.conf <<'EOF'
[Unit]
RequiresMountsFor=/large/microvm-mcp
Wants=network-online.target
After=network-online.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now microvm-mcp.service
```

The packaged unit uses `Restart=no` and a 15-minute stop timeout on purpose:
on shutdown it checkpoints running VMs. Raise `TimeoutStopSec` in the drop-in
for large VM counts. Don't add sandboxing directives (see the comment in the unit).

A non-loopback listener also needs the port open in the host firewall. ufw
denies incoming connections by default here:

```sh
sudo ufw allow 1050/tcp comment 'microvm-mcp (cleartext HTTP)'
```

## 6. Verify

Startup verifies every image digest and probes a reflink before it listens.
That takes a few seconds.

```sh
systemctl is-enabled microvm-mcp.service
journalctl -u microvm-mcp.service -n 20     # expect one "listening on" line per address
sudo ss -ltnp | grep 1050                   # 0.0.0.0:1050 and [::]:1050
```

An end-to-end check with a throwaway workspace:

```sh
sudo microvm-mcp issue-token --state-dir /large/microvm-mcp \
  --workspace install-check --output /root/install-check.token --ttl 900
H="Authorization: Bearer $(sudo cat /root/install-check.token)"
curl -sS -H "$H" -d '{"request_id":"c1","network":"off"}' http://127.0.0.1:1050/v1/tools/vm_create
# The VM ID is the "id" field.
curl -sS -H "$H" -d '{"vm_id":"<id>","request_id":"e1","command":"uname -r; id -u"}' \
  http://127.0.0.1:1050/v1/tools/vm_exec
curl -sS -H "$H" -d '{"vm_id":"<id>","job_id":"<job_id>"}' http://127.0.0.1:1050/v1/tools/vm_job_output
curl -sS -H "$H" -d '{"vm_id":"<id>"}' http://127.0.0.1:1050/v1/tools/vm_destroy
sudo microvm-mcp revoke-workspace --state-dir /large/microvm-mcp --workspace install-check
sudo rm /root/install-check.token
```

**Destroy the VMs before revoking.** A revoked workspace can't be reissued a
token, and there is no admin destroy command, so its VMs become unreachable
through the API.

For full qualification, stop the service (the smoke test needs the state
lock) and run the installed-package `scripts/smoke.py` command from the
packaging guide, then start the service again.

## 7. Clients

Point MCP clients at `http://<host>:1050/mcp` and HTTP clients at
`http://<host>:1050/v1/tools/<tool>`, with the workspace bearer token. `<host>`
must be in `allowed_hosts`.
Clients still configured for the old port, or adapter manifests generated with
`--endpoint` for it, must be updated.
