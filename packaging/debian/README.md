# Debian vendor packages

The initial target is **Debian 13 (trixie), amd64**, with systemd, real KVM,
cgroup v2 and an XFS/Btrfs volume supporting reflinks. These are vendor binary
packages for a private APT repository, not a Debian archive submission.

## Build

On an amd64 machine with Docker:

```sh
scripts/build-debs
```

The resulting three `.deb` files, `.changes`, `.buildinfo` and `SHA256SUMS` are
written to `dist/debian/`. An optional argument selects another output directory.
The source is mounted read-only; containers receive no KVM device or host secrets.
The build runs as the invoking UID and does not install anything on the host.

The builder uses a digest-pinned Debian image. Debian build tools are installed
from current trixie repositories: the complete build is **not bit-reproducible**.
`artifacts.json` pins Python 3.13.15 (python-build-standalone 20260901), its license
sources, Firecracker/jailer 1.17.0, and build-only uv 0.9.4 by SHA-256.
Application and test wheels come from `uv.lock`; hashes are mandatory and source
builds are forbidden. The private application is installed from `src/`, with
its dependency metadata and notices preserved. Neither pip nor uv ships on the
server. GUI modules, development headers and installer tools are omitted from
the private runtime. Python's bundled libraries receive updates through our runtime package,
not through Debian's system Python updates.

The online preparation step precedes `dpkg-buildpackage`. Preparation writes an
inventory of file hashes, symlinks and modes; the offline build rejects stale or
modified inputs. For a manual build in an equivalent Debian 13 environment:

```sh
python3 scripts/debian.py prepare
dpkg-buildpackage -b -us -uc
```

`debian/rules` runs Ruff lint/format and pytest against the **bundled** Python and
locked wheels. It computes ELF library dependencies using `dh_shlibdeps`, leaves
vendor binaries unstripped, and uses debhelper for conffiles and systemd scripts.
Lintian errors fail `scripts/build-debs`; warnings must be reviewed. Scoped,
commented overrides cover intentionally embedded Python libraries and retained
upstream ELF symbols, static PIE executables, and a non-executable stdlib module.
The upstream Python executable currently produces a `hardening-no-pie` warning;
we preserve that upstream binary rather than claiming Debian compiler hardening.
Initial vendor releases also have no Debian bug number to close. This flow
produces binary packages; it does not claim a self-contained Debian source
package. Retain the repository revision, lockfile and pinned upstream sources
with releases. Build inputs in `.artifacts/debian` can be removed and refetched.

## Install and configure

```sh
sudo apt install ./microvm-mcp_*.deb ./microvm-mcp-python_*.deb \
  ./microvm-mcp-firecracker-1.17_*.deb
```

The service is neither enabled nor started by installation. The packages install:

| Path | Purpose |
| --- | --- |
| `/usr/bin/microvm-mcp` | Administrator CLI and service launcher |
| `/usr/bin/microvm-mcp-install-image` | Local digest-checked image provisioning |
| `/usr/lib/microvm-mcp/` | Private application, dependency modules and Python |
| `/usr/lib/microvm-mcp/firecracker/1.17.0/` | Unmodified Firecracker/jailer pair |
| `/etc/microvm-mcp.toml` | Operator-edited conffile |
| `/usr/lib/systemd/system/microvm-mcp.service` | Disabled systemd unit |

Provision a dedicated reflink-capable volume at `/var/lib/microvm-mcp` (or change
`state_dir`). Packages deliberately do not mount or format storage, enable IPv4
forwarding, allocate jail UID ranges, or issue workspace credentials.
Create the mounted state directory with mode 0700. Reserve the configured jail
UIDs and capacity; see the main README's host setup requirements.

Build the guest rootfs on a build machine using `scripts/build-image`, and obtain
a trusted Firecracker-compatible kernel. Transfer both artifacts to the server
along with their independently verified SHA-256 digests. The installer takes
local files; it does not download arbitrary images or execute guest code:

```sh
sudo microvm-mcp-install-image \
  --state-dir /var/lib/microvm-mcp --image-id debian-13-dev-v1 \
  --rootfs /srv/releases/debian-13.ext4 \
  --rootfs-sha256 VERIFIED_ROOTFS_SHA256 \
  --kernel /srv/releases/vmlinux \
  --kernel-sha256 VERIFIED_KERNEL_SHA256
```

It preserves sparse zero regions, verifies both copies, tests a reflink from the
image into the state directory, and publishes a new bundle under `images/` only
after success. Existing bundles are never updated in place. Add its printed
`[images.<id>]` table to `/etc/microvm-mcp.toml` and mark the image `vm_create`
uses by default with `default = true`. Several images can be installed and
configured side by side; a top-level `image_id`/`rootfs`/`kernel` configuration
from earlier releases keeps working as one default image. Then configure quotas, UID range, and
optional networking. The default listener remains on loopback.

```sh
sudo microvm-mcp issue-token --state-dir /var/lib/microvm-mcp \
  --workspace example --output /run/microvm-example.token --ttl 86400
sudo systemctl enable --now microvm-mcp.service
sudo journalctl -u microvm-mcp.service
```

The launchers use an isolated Python interpreter and ignore Python environment
overrides and user site packages. Systemd sets the Debian CA bundle for outbound
TLS. Credentials remain on the host; no token is packaged or baked into images.

## Upgrade, rollback and removal

Plan a maintenance window, stop the service, upgrade, then explicitly start it:

```sh
sudo systemctl stop microvm-mcp.service
sudo apt install ./microvm-mcp_*.deb ./microvm-mcp-python_*.deb \
  ./microvm-mcp-firecracker-1.17_*.deb
sudo systemctl start microvm-mcp.service
```

Application upgrades also stop the service before unpacking and leave it stopped.
Pre-install scripts stop it before replacing the application or dependencies, since APT
can unpack those first. The unit signals the controller first and allows up to
15 minutes for graceful checkpointing; use a systemd override to increase this
for the configured VM count and storage performance. Check shutdown logs for
checkpoint failures. Do not assume that a forced termination preserves jobs.

Firecracker is an exact dependency and retains its original binary digest. A
future VMM version requires an explicit compatibility/migration decision:
checkpoints cannot automatically resume under a different binary. Merely keeping
old binaries installed does not enable mixed versions in one service instance.
Keep the executable basename `firecracker` stable because it is part of jail
paths. Python runtime security updates can be released within the 3.13 series;
changing Python minor versions also requires rebuilding the application wheels.
Increment the relevant package revision whenever its payload or metadata changes;
never replace a published APT package with different bytes at the same version.
Firecracker package revision changes also require updating the application's
exact dependency in `debian/control`.

Guest changes require a rebuilt image, a new ID and digest, and newly created
VMs. Existing guests/checkpoints retain their old supervisor. Package downgrade
does not roll back workspace data or promise database/checkpoint compatibility;
coordinate recovery with retained artifacts and backups.

Removal stops the service. Purge removes packaged configuration, but **VM state,
images, checkpoints and workspace credentials are retained**. Delete those only
as an explicit operator action. Use a signed APT repository for distribution;
repository publication and signing keys are outside this local build workflow.

## Release validation

In addition to the bundled-Python unit checks and Lintian, install, upgrade,
remove and purge the packages in a disposable Debian 13 system. Verify that
configuration edits and VM data survive, the service is initially disabled,
and the packaged launchers work without a system Python dependency.

`scripts/test-debs` performs the container portion of these checks, including a
reinstall through dpkg's upgrade path, configuration/state retention and Python
environment isolation. It requires Docker and the built packages, and makes no
changes to the host installation. It does not boot systemd as PID 1.

On a provisioned Linux KVM host, validate the packaged interpreter/application
with `scripts/smoke.py` using the main README's network and 30-second suspension
probes. Stop the service first to release its exclusive state lock. Include a
systemd stop/start with running and paused VMs to qualify checkpoint behavior.
Container package checks alone do not qualify VM operation or systemd shutdown.

From this repository, the installed-package smoke command is:

```sh
sudo /usr/lib/microvm-mcp/python/bin/python3.13 -I -B -c \
  'import runpy, sys; sys.path[:0] = ["/usr/lib/microvm-mcp/app", "/usr/lib/microvm-mcp/dependencies"]; runpy.run_path(sys.argv.pop(1), run_name="__main__")' \
  scripts/smoke.py --config /etc/microvm-mcp.toml --network --clock-suspend-seconds 30
```

This imports the installed application and dependencies, not a development venv.
