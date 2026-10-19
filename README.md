# microvm-mcp

A standalone first pass at private Linux development workspaces over MCP and HTTP.
One workspace credential can create and operate its own Firecracker microVMs.
Commands execute in a full Debian guest, never in the service process.

This directory is its own Python package, dependency lockfile, image recipe, CLI,
and test suite. It can move into another repository unchanged. It imports nothing
from fast-agent. The optional fast-agent integration generates configuration for
the existing `host_fetch` transport; no harness or demo code changes are required.

## Implemented

- Official MCP Python SDK, Streamable HTTP at `/mcp`, plus matching JSON HTTP
  operations at `POST /v1/tools/{tool_name}`. Both share validation and authorization.
- Host-issued opaque workspace tokens, SHA-256 token hashes in SQLite, expiration,
  rotation without changing ownership, and workspace revocation. Every request is
  authenticated; every VM/job/file operation checks workspace ownership.
- Operator-configured guest images, chosen per VM. The recipe builds a Debian 13
  image with Python, Node, Git, Bash, GCC and standard build tools. Guest commands
  run as UID 1000. Customize the trusted image recipe to install system packages;
  the API does not grant root or passwordless sudo to commands.
- One jailed Firecracker process and cgroup per VM. Distinct jail UIDs, private
  network namespaces, CPU/memory/PID limits, no host-directory mounts in the guest.
- Reflink CoW raw ext4 disks on an XFS/Btrfs host filesystem. Unsupported storage
  fails preflight. Guest disks are fixed at the image's size (3 GiB in the recipe).
- Async command handles; separate stdout/stderr returned as readable UTF-8 text;
  a shared output bound; cancellation/timeouts
  cover descendants through guest cgroups, including processes that call setsid.
- Workspace file transfer using base64, bounded to 128 KiB per call, with symlink
  traversal refused. Larger artifacts move over a guest stream.
- Host-to-guest byte streams over vsock, authorized like every other operation,
  for MCP servers in the guest, operator shells, VNC and bulk transfer, with
  guest networking off.
- Manual pause (retains RAM), hibernate (full memory/device checkpoint, releases
  the VMM process), resume, automatic idle suspension, and automatic wake on work.
- Network off by default. Network on adds IPv4 NAT with guest-source enforcement,
  private/metadata destination denial and inbound denial. No guest IPv6 egress.
- Durable metadata and creation/execution retry receipts. A transport-uncertain
  execution is not replayed automatically. `vm_status.jobs` exposes known handles
  for reconciliation; the history is capped at 4096 requests per workspace.

## Debian packages

The project builds three packages for **Debian 13 (trixie), amd64**:

| Package | Contents |
| --- | --- |
| `microvm-mcp` | Service, locked Python dependencies, administrator CLI, image installer, configuration and systemd unit |
| `microvm-mcp-python` | Private CPython 3.13.15 runtime; leaves the system Python unchanged |
| `microvm-mcp-firecracker-1.17` | Pinned Firecracker and jailer 1.17.0 binaries |

Build from the repository root on an amd64 machine with Docker and network access:

```sh
scripts/build-debs
# Optional: choose another output directory.
scripts/build-debs /absolute/path/to/packages
```

The default output directory is `dist/debian/`, containing the three `.deb` files,
`.changes`, `.buildinfo` and `SHA256SUMS`. The builder verifies pinned upstream
archives and dependency hashes, then runs Ruff, pytest and Debian packaging in
a container with networking disabled. Lintian checks the resulting packages.
Building does not install packages on the host. To test installation, reinstall,
removal and state retention in a disposable Debian container:

```sh
scripts/test-debs
```

Copy the three packages to the server and install them with APT:

```sh
sudo apt install ./microvm-mcp_*.deb ./microvm-mcp-python_*.deb \
  ./microvm-mcp-firecracker-1.17_*.deb
```

Installation requires no Python downloads or compilation; APT may fetch ordinary
system dependencies. The service remains disabled and stopped until configured.
Provision KVM, cgroup v2, reflink-capable storage and a separately built guest
kernel/rootfs, then edit `/etc/microvm-mcp.toml` before enabling the service.
See the [Debian packaging guide](packaging/debian/README.md) for image installation,
startup, upgrade behavior, runtime maintenance and validation details.

## Kubernetes

`deploy/k8s/` builds a container image from these packages and runs it as a
privileged, node-pinned StatefulSet with local storage and a TLS sidecar. The
host requirements below still apply to the node. See the
[Kubernetes deployment guide](deploy/k8s/README.md).

## Host setup

For installation directly from source, follow the setup below.

The validated target is Linux x86_64 with real `/dev/kvm`, cgroup v2, matching
**Firecracker and jailer 1.17.0**, Python 3.11+, `ip`, `nft`, and `iptables`.
Firecracker 1.16 has a vsock pause/resume defect; preflight refuses versions older
than 1.17. The service currently runs as root to provision jails and networking.
Keep its listener on loopback; put authenticated remote access behind TLS.

Install Firecracker/jailer from the operator-verified
[upstream release](https://github.com/firecracker-microvm/firecracker/releases/tag/v1.17.0).
Supply an operator-pinned Firecracker-compatible Linux kernel with ext4,
virtio-block, virtio-net, virtio-vsock and cgroup/PID-controller support built in.
The real smoke used the Firecracker CI x86_64 `vmlinux-6.1.155` kernel.
Kernel and rootfs SHA-256 digests are checked before serving.

Use a dedicated reflink-capable volume for the base image and state: reflinks
must stay on one filesystem. Reserve enough real capacity for every VM's fully
written disk and two RAM checkpoint generations, even though new disks share
blocks. Set `max_storage_gib`, `max_memory_mib`, and `max_vms` to the host budget;
these are conservative reservations across all retained VMs, including hibernated
VMs. Admission and checkpointing also check available disk space. This is not a
substitute for storage isolation from unrelated host services.

```sh
uv sync --locked
# Docker and mkfs.ext4 are needed only to build the image. Preserve guest UIDs.
sudo scripts/build-image /var/lib/microvm-images
sudo install -d -m 0700 /var/lib/microvm-mcp
```

Copy `examples/service.toml`, set the binary/kernel/image paths, and replace the
digest placeholders using `sha256sum`. The image digest pins the deployed image;
the Docker recipe uses Debian package repositories and is not a bit-reproducible
historical rebuild.

### Guest images

Each `[images.<id>]` table is one bootable image. `rootfs` and `kernel` default to
`<state_dir>/images/<id>/rootfs.ext4` and `vmlinux`, the layout written by
`microvm-mcp-install-image`, which prints the table to paste. One image is
marked `default = true`; it boots when `vm_create` names none.

```toml
[images.debian-13-dev-v1]
rootfs_sha256 = "..."
kernel_sha256 = "..."
default = true

[images.re-analysis-v1]
rootfs_sha256 = "..."
kernel_sha256 = "..."
min_memory_mib = 4096  # refuse smaller VMs; also the default memory_mib
# memory_mib = 6144    # default when vm_create omits memory_mib
```

Every configured image's rootfs and kernel digests are verified, and a reflink is
probed, before the service accepts requests. Unknown keys and malformed IDs fail
startup. A configuration with the older top-level `image_id`, `rootfs`, `kernel`,
`rootfs_sha256` and `kernel_sha256` still works as one default image; mixing both
forms is an error. `vm_create` advertises the configured IDs as an enum, and
`vm_status` reports each VM's image.

A VM copies its kernel and clones its disk at creation, so pause, hibernate and
resume never read the image again. Removing an image from the configuration does
not strand its paused or hibernated VMs; the service logs them at startup, and
they can be resumed, used and destroyed, but no new VM can boot that image.
Restoring still requires the Firecracker binary the VM was created with. Storage
reservations use each image's own rootfs size, recorded on the VM. Reserve unused host UIDs starting at `uid_base`, one per VM
slot. Do not reuse this range for host users or another service instance.

Networking needs operator-enabled IPv4 forwarding and an explicit `uplink`.
The service uses `10.231.<slot>.0/30` and `10.232.<slot>.0/30`; these ranges must not
overlap existing routes. For each online VM it adds a private network namespace,
veth/TAP, route, nftables isolation/NAT table and narrowly scoped iptables FORWARD
rules. It removes those on hibernation or deletion. It never flushes host rules or
changes the host's default firewall policies. Add public management networks to
`blocked_cidrs`; private-address filtering cannot recognize a public control plane.

```sh
sudo .venv/bin/microvm-mcp issue-token \
  --state-dir /var/lib/microvm-mcp --workspace demo-alice \
  --output /run/microvm-alice.token --ttl 86400 --allow-network
sudo .venv/bin/microvm-mcp serve --config /etc/microvm-mcp.toml
```

`--image ID` (repeatable) restricts a workspace to those images, the same way
`--allow-network` grants networking; without it the workspace may use any
configured image. A refused image returns 403 `forbidden`.

Issuance is a local administrator action, not an MCP tool. Token files are 0600
and are never printed. Give the trusted client/broker read access to its token
file. Reissuing for the same workspace creates another token and preserves its
original quota/policy; it does not revoke the old token or widen permissions.
Do not put tokens in model prompts, tool arguments, guest environments or images.

```sh
sudo .venv/bin/microvm-mcp revoke-workspace \
  --state-dir /var/lib/microvm-mcp --workspace demo-alice
```

Revocation prevents future access, including refresh by this CLI, and closes
open guest streams within 5 seconds. Existing jobs are not automatically killed. An operator needing immediate containment must stop
the workspace's VM cgroups as well. An integrated revoke-and-stop admin command is
not implemented. Credential expiry likewise does not delete VM state.

## MCP and HTTP clients

Configure an HTTP MCP client with URL `http://127.0.0.1:8090/mcp` and the workspace
credential in its `Authorization: Bearer ...` header. The SDK handles modern and
legacy protocol negotiation. This deployment uses provisioned bearer credentials;
OAuth login/discovery and interactive client registration are not implemented.

For ordinary HTTP clients, send the same arguments as JSON to the named operation:

```json
POST /v1/tools/vm_create
{"request_id":"workspace-create-1","network":"off","memory_mib":1024,"vcpus":2}

POST /v1/tools/vm_exec
{"vm_id":"<returned id>","request_id":"build-1","command":"python3 -c 'print(2+2)'"}
```

Requests require the authorization header; errors use non-2xx status codes and
`{"error":{"code":...,"message":...}}`. MCP tool errors use `isError=true`.
Creation and execution require `request_id`. Reuse the same ID and arguments for
a retry. A reused ID with changed arguments is refused. An interrupted operation
with an unknown outcome is also refused instead of executing again.

Tools: `vm_create`, `vm_list`, `vm_status`, `vm_exec`, `vm_job_status`,
`vm_job_output`, `vm_job_cancel`, `vm_read_file`, `vm_write_file`, `vm_suspend`,
`vm_resume`, `vm_destroy`. Discovery advertises their complete JSON schemas.
`vm_create` takes an optional `image` from the configured IDs.
File paths and command working directories are relative to `/workspace`.
There are at most four active jobs and 256 total jobs per VM.

### Reading command output

`vm_exec` returns a `job_id`. Call `vm_job_output` while the job runs or after it
finishes. Its default response contains separate, readable `stdout` and `stderr`
strings; an LLM does not need to decode base64 to read command results:

```json
{
  "job_id": "<returned job id>",
  "encoding": "utf-8",
  "stdout": "0, 1, 1, 2, 3, 5\n",
  "stderr": "",
  "next_cursor": {"stdout": 17, "stderr": 0},
  "decoding_errors": {"stdout": false, "stderr": false},
  "state": "completed",
  "exit_code": 0,
  "truncated": false,
  "timed_out": false
}
```

Omit `cursor` on the first call. Pass the entire returned `next_cursor` object
as `cursor` on the next call, alongside `vm_id` and `job_id`. Each cursor field
is a byte offset in that stream, not a character count. Omitted stream offsets
start at zero. Reading is non-destructive: repeating a cursor rereads the same
retained bytes. Once the job is completed, continue reading until neither offset
advances. An empty response while the job is running is not end-of-output.

The guest supervisor drains both pipes independently. Up to **256 KiB total** is
retained across the two streams, with at most **16 KiB per stream per call**.
Excess output is drained and discarded, with `truncated=true`; it does not block
the command. Ordering within each stream is preserved; there is no ordering
guarantee between stdout and stderr. Use `python3 -u` or explicit flushing when
you want prompt output from Python.

Text decoding preserves whitespace, line breaks, and tracebacks. Incomplete UTF-8
characters at a page boundary or at the current end of a running stream wait for
the next bytes. Invalid bytes, including incomplete characters at EOF or the
retention limit, become `�`; `decoding_errors` flags the affected streams for
that response. A literal valid `�` does not set the flag.

For exact binary output, request `"encoding":"base64"`. The response then has
`stdout_base64` and `stderr_base64` instead of the text fields, and omits
`decoding_errors`. The same byte cursors and limits apply. File transfer tools
continue to use base64.

The host retrieves output over Firecracker's Unix-socket-to-vsock bridge to the
supervisor on guest port 1024, then returns it through the authenticated HTTP/MCP
API. Guest networking can remain off. Workspace credentials stay on the host.

**Upgrade note:** this replaces the previous combined `data_base64` output and
integer cursor. Update clients and regenerate optional adapter manifests. Rebuild
the guest image, pin its new SHA-256 in the service config, and create new VMs.
Existing guests and restored checkpoints keep their old supervisor; restarting
the host service alone does not upgrade them.

## Guest streams

A host client can open a two-way byte stream to a vsock port in an owned VM:

```text
GET /v1/vms/{vm_id}/streams/{port}   (WebSocket upgrade)
Authorization: Bearer <workspace credential>
```

The service connects through the VM's Firecracker vsock socket, the same path as
the control channel, so it works with `network=off`. Connections go host to guest
only. A program in the guest listens on its own port; nothing listens by default.
For example, started with `vm_exec`:

```sh
socat VSOCK-LISTEN:5000,fork EXEC:'my-mcp-server --stdio'   # if socat is in the image
```

The `fork` option starts a fresh process per connection. A long-lived server that
holds state across reconnections, such as a Ghidra session, should accept
connections itself.

Requests pass the same host allowlist, no-`Origin`, bearer and ownership checks as
the tools. Refusals happen before any guest connection: 401 for credentials, 404
for another workspace's VM, 403 for port 1023 (the job broker pipe), port 1024
(the guest supervisor), or ports outside `stream_ports`, 409 for a full stream
quota or no guest listener. Opening
a stream wakes a suspended VM. Binary messages carry bytes; text messages are
sent as their UTF-8 bytes. Client messages are limited to 1 MiB.

- **EOF.** An empty binary message from the client is end-of-file for the guest.
  Firecracker's vsock bridge has no half-close: the guest reads the remaining data
  and then EOF, but the connection then closes in both directions, so a guest
  cannot reply after it. Protocols that need a reply after the input ends need an
  in-band end marker. The stream closes with code 1000 when the guest closes its
  end.
- **Lifecycle.** An open stream counts as activity, so automatic idle suspension
  waits for it to close. Manual suspend refuses with `busy` unless `force=true`.
  Any suspension or destroy closes the VM's streams with code 1001 and reason
  `suspended` or `destroyed`. Guest listeners survive; clients reconnect after
  resume. Credentials are rechecked every 5 seconds, and revocation or expiry
  closes the stream with 1008. `vm_status` reports the number of open streams.
- **Limits.** Configure them in the service TOML (defaults shown):

  ```toml
  stream_ports = [[1025, 65535]]   # inclusive ranges; [] disables streams; 1023 and 1024 are refused
  max_streams = 32                 # host-wide
  max_streams_per_workspace = 8
  max_streams_per_vm = 4
  stream_rate_bytes = 33554432     # per direction per stream; 0 is unlimited
  ```

  Streams grant no authority beyond `vm_exec`, which already moves data into and
  out of the workspace's own VM, so there is no separate token grant.
- **Logging.** Open and close are logged with workspace, VM, port, duration, byte
  counts and close reason, never payload.

`microvm-mcp attach` bridges stdin/stdout to a stream. That makes a guest stdio
MCP server usable as an ordinary MCP client's command. The token is read from a
file and never appears on the command line:

```sh
microvm-mcp attach --endpoint http://127.0.0.1:8090 \
  --token-file /run/microvm-alice.token --vm <vm_id> --port 5000
# Upload: the guest program reads EOF after the file.
microvm-mcp attach ... --port 5001 < archive.tar
# Download: keep the stream open after stdin ends; the guest closes it.
microvm-mcp attach ... --port 5002 --ignore-stdin-eof < /dev/null > out.bin
```

It exits 0 when the guest closes normally and 1 on refusal or any other close.

## Job broker pipe

`vm_exec` accepts an optional `broker_pipe_token`: a one-time unguessable token
(`secrets.token_urlsafe(32)` or another 43–128 character `[A-Za-z0-9_-]` value).
The service stores only its SHA-256, in memory. The token is not written to
receipts, VM status, logs, or the guest, and a retried `request_id` does not
replace the hash. The guest is told `broker_pipe: true` and the job id. The
response adds `"broker_pipe": true`.

For that job the guest supervisor listens on `/run/fa-broker/broker.sock` inside
a mount namespace private to the job, mode `0660` and group 1000, so uid 1000 in
that namespace can connect and other jobs cannot see the socket. The job
environment is the workspace broker client, with a non-secret placeholder
capability and no storage credentials:

```text
FA_WORKSPACE_BACKEND=broker
FA_WORKSPACE_BROKER_URL=http://127.0.0.1/v1/call
FA_WORKSPACE_BROKER_CAPABILITY=fa-broker-placeholder
FA_WORKSPACE_MAX_OBJECT_BYTES=67108864
```

`FA_WORKSPACE_SCOPE` is unset. The HTTP target is `POST /v1/call` on
`/run/fa-broker/broker.sock`. Guest networking stays off. A client that opens
that socket and writes the broker request, including the placeholder bearer
token, is carried to the host unchanged.

Each accepted socket is forwarded to the host on Firecracker's guest-to-host
vsock `<uds_path>_1023` (port 1023). The first line is the 32-hex job id; the
rest is raw bytes. Port 1023 is not a public stream and is not served at
`/v1/vms/{vm_id}/streams/{port}`.

The controller that knows the token attaches:

```text
GET /v1/vms/{vm_id}/jobs/{job_id}/broker   (WebSocket upgrade)
Authorization: Bearer <workspace credential>
X-Broker-Pipe: <broker_pipe_token>
```

The same host allowlist, no-`Origin`, and bearer checks apply. A wrong token,
another workspace, an unknown job, or a VM without this pipe is 404. A second
concurrent attach is 409. WebSocket messages are binary. The payload is a byte
stream of big-endian frames, which a message may split or combine:

| Kind | `type` | Layout |
| --- | --- | --- |
| open | 1 | `u8 type, u32 conn_id` |
| data | 2 | `u8 type, u32 conn_id, u32 length, payload` with `length <= 65536` |
| close | 3 | `u8 type, u32 conn_id` |

`conn_id` is nonzero. The service assigns it and sends `open` when a guest
connection arrives. The controller sends `data` and `close` only. The service
copies bytes and does not interpret them. The controller splices its end to the
local broker and replaces `Authorization: Bearer fa-broker-placeholder` with the
session capability. That splice stays outside this service.

An open pipe counts as activity, so idle suspension waits for it. Manual suspend
refuses with `busy` unless `force=true`. Suspend and destroy close the pipe with
WebSocket code 1001 and reason `suspended` or `destroyed`. Credentials are
rechecked every 5 seconds; revocation or expiry closes it with 1008. Close and
open are logged with workspace, VM, job, duration, byte counts, and reason,
never the token or payload. The hash does not survive a service restart.

**Upgrade note:** existing guest images ignore `broker_pipe` until the image is
rebuilt and its digest is pinned. Restarting the host service does not upgrade a
running guest or a restored checkpoint. Old images keep working; they just do
not open the socket.

## Suspension and failure semantics

Creation defaults to hibernating after 300 idle seconds. Set `idle_seconds=0` to
disable it, or choose `idle_mode="pause"`. Active managed commands, including
detached descendants, prevent automatic suspension. Status polling does not keep
an otherwise idle workspace awake. Background system daemons are not counted as
user activity. This is a scheduling heuristic, not a host security boundary.

Manual suspend defaults to `mode="hibernate"` and refuses active jobs. With
`force=true`, it freezes them. VM status, list and cached job status never wake a
suspended VM. Execution, file transfers, output retrieval and cancellation do.
Cancellation of a frozen job resumes the guest before terminating its cgroup.
Managed command timeouts exclude suspension; suspension can extend wall time.

Before stopping vCPUs, the supervisor freezes the managed jobs' entire cgroup
subtree and pauses timeout accounting. On boot and every resume, the host sends
its current wall time through the private vsock control channel. The guest sets
`CLOCK_REALTIME` before thawing jobs, and restores their remaining timeout budgets
under the supervisor lock before timeout checks resume. This works with both
`tsc` and `kvm-clock`, including network-off VMs. Failed synchronization stops
the VMM and marks the VM failed.
Old images/checkpoints without this lifecycle protocol are rejected; rebuild and
pin the guest image and create new VMs when upgrading.

Firecracker's `clock_realtime` snapshot option is deliberately disabled: with
`kvm-clock`, it also advances monotonic time across a restore. Wall time is instead
corrected separately, without changing the monotonic clock. This is a correction
at boot/resume, subject to local control-channel latency, not continuous NTP/PTP
synchronization; the host clock must itself be accurate. Applications' own
timers and sleeps still follow guest kernel semantics: plain pause advances
monotonic time, while hibernation normally excludes time spent stopped. The
supervisor compensates its managed command timeouts for either behavior, but
does not rewrite application timers. Wall-clock deadlines can expire during
suspension.

Hibernation pauses the guest, writes a full memory/device checkpoint, drains disk
I/O, retains the corresponding private disk, fsyncs checkpoint metadata, stops
the process and removes its network resources. Resume restores that same VM on
the same host. It does not clone a checkpoint into a second VM or promise live
TCP connections survive. The snapshot's Firecracker binary digest is checked.
Never modify a hibernated VM disk externally or restore stale memory over a disk
that has since run. The state machine marks interrupted mutations failed rather
than silently rolling back to such a checkpoint.

Graceful service shutdown checkpoints running/paused VMs, including active jobs.
After a service crash, hibernated VMs remain restorable; other VMs are marked
failed and leftover processes are killed through their owned cgroups. They can
be inspected/deleted, but automatic recovery of in-flight commands is deferred.
There is no HA, cross-host migration, VM sharing, built-in interactive terminal
(a stream can carry one that the guest provides), inbound web preview, automatic VM retention expiry, or public production deployment in
this first pass. Guest roots/checkpoints are sensitive workspace data and remain
inside the service's private state directory.

## Optional fast-agent-demo integration

The adapter uses **host_fetch**, so the host owns the token and TLS. The service
does not know about fast-agent sessions, manifests, state envelopes or the VMM.
Generate a fragment and append it to the demo's existing tool manifest:

```sh
.venv/bin/microvm-mcp demo-manifest \
  --endpoint http://127.0.0.1:8090 --vm-class exec > microvm-tools.fragment.toml
```

The fragment assumes that the existing manifest defines an `exec` class (the
packaged demo does). That class is schema-required but inert for host_fetch.
Add this entry to the host's **existing** private secrets file:

```toml
[secrets.microvm_workspace]
file = "/run/microvm-alice.token"
```

Run the demo with that manifest, `--secrets`, `--stdio-tools` and `--expose-tools`,
as its normal launcher already does. Use an HTTPS endpoint and a matching service
Host allowlist when the service is remote. The returned tool output has the normal
fast-agent host_fetch wrapper, `{"status":200,"body":{...}}`.

**One static demo credential means one workspace for all sessions in that demo
instance.** It must not be presented as per-user isolation on a shared public demo.
For the first demo, use one separately configured demo instance per workspace.
Shared multi-user hosting needs a future host-side adapter that derives workspace
identity from authenticated context and chooses its credential. A caller-supplied
`session_id`/`workspace_id` is not authorization. Neither service transport accepts
an ownership override, and the generated tool schemas contain no token field.

`examples/tool_probe_agent.py` exercises this path without an LLM. Boot it with
fa-serve and the generated manifest, then send a turn with
`{"input":{"tool":"vm_create","args":{"request_id":"probe-1"}}}` and a
normal front-end session ID. Its purpose is integration validation only.

## Validation

```sh
uv run --locked ruff check src guest tests scripts examples
uv run --locked ruff format --check src guest tests scripts examples
uv run --locked pytest
# Run on a provisioned host. Missing KVM, jailer, reflinks etc. are hard failures.
sudo .venv/bin/python scripts/smoke.py --config /etc/microvm-mcp.toml --network
# Longer suspension probes, including jobs with a three-second timeout:
sudo .venv/bin/python scripts/smoke.py --config /etc/microvm-mcp.toml \
  --network --clock-suspend-seconds 30
```

The unit suite uses a fake backend that cannot execute code. The separate real
smoke boots jailed VMs, checks Fibonacci output, separate streams, UTF-8 pagination,
binary output, and output limits, compiles generated C, checks disk isolation, preserves a
running process across suspension, checks wall time and managed timeouts across
pause, hibernation, and pause-to-hibernate transitions, checks automatic
hibernation, verifies network policy, and denies cross-workspace access. For
every configured image it boots a VM and checks the kernel copy's digest and
the guest root filesystem's identity across hibernate/resume. With two or more
images, it checks that a token restricted to one is refused for the other. It runs
an MCP stdio round trip through `attach` and a guest vsock stream, copies 100 MiB
each way, and checks refusals, half-close behavior and stream closure on
hibernation. Run it with the legacy single-image configuration and with an
`[images]` configuration listing two distinct images. It deletes
its VMs and revokes its credentials in cleanup. See `VALIDATION.md` for measured
results and the history of smoke failures and fixes.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
