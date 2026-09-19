# Validation

## UTF-8 output update — 2026-09-19

- Ruff lint and format checks passed; pytest: **58 passed**, no skips, with the
  same upstream Starlette/AnyIO deprecation warning noted below.
- The locked dependencies checked successfully. Source distribution and wheel
  builds passed; both include LICENSE, and the wheel declares Apache-2.0.
- Rebuilt the Debian guest with the updated supervisor. Rootfs SHA-256:
  `014aa7d13f1dbb4cb2700dae9fc6af9c95d48d433a5f098f20baaca6654a2b4b`.
  Used the same Firecracker 1.17.0 and pinned kernel as below, on the host's
  dedicated XFS volume.
- The real KVM smoke passed **13 checks**, including Fibonacci with separate
  stdout/stderr, nonzero exit status, UTF-8 pagination, invalid-byte replacement
  flags, exact binary retrieval, and draining both pipes past the shared cap.
  Boot took 1.830 s; restore took 0.131 s; process memory and output survived.
- **The full smoke did not pass:** it stopped at the existing assertion that
  guest wall time is within two seconds of host time after restore. This also
  failed on the unchanged code and old image earlier on 2026-09-19. Later
  cancellation, automatic hibernation, network-on, and revocation assertions
  were not reached in this run. The clock failure remains unresolved.
- A separate real-VM test through a temporary loopback HTTP server retrieved
  all 20 Fibonacci values as readable stdout, a separate stderr marker, exit
  code 0, and no timeout/truncation. An intentional error returned stderr and
  exit code 7. The same Fibonacci check also passed over the MCP endpoint.
- Test VMs were destroyed, temporary credentials revoked, and the temporary
  HTTP server stopped. These results do not qualify deployment of the old
  supervisor: rebuild/pin the image and recreate VMs when upgrading.

## First-pass validation — 2026-09-18

The service was tested on the actual Linux host outside the tool filesystem/device
sandbox. No KVM-dependent checks were counted as passes without executing them.

### Environment

- Host: Linux 7.0.0-30-generic, x86_64, openable `/dev/kvm`, cgroup v2.
- Firecracker and jailer: 1.17.0, installed in `/usr/local/bin` by the operator.
- Guest: built Debian 13 development image, Linux 6.1.155 Firecracker CI kernel.
- Storage: temporary 8 GiB loop-mounted XFS filesystem with reflinks; host root is
  ext4 and was not silently used as a CoW substitute.
- MCP SDK: 2.2.0 from the checked-in `uv.lock`.

Artifact SHA-256 digests for this run:

| Artifact | Digest |
| --- | --- |
| Firecracker | `99ad0f5cd0514a88aad0e9ae8cfdb3cc3b4ab9d190e1194602406c786b5de7a5` |
| Jailer | `65ef226e96f0ceda55ba643f445801ef2cc0ea667ef67cad8ac4f406c9c8434f` |
| Guest kernel | `e20e46d0c36c55c0d1014eb20576171b3f3d922260d9f792017aeff53af3d4f2` |
| Built rootfs | `ad672e003bdb285f8c76833ad06d232ac308aeb506534b897eb545370992da54` |

### Portable checks

- `ruff check src guest tests scripts examples`: passed.
- `ruff format --check src guest tests scripts examples`: passed.
- `pytest -q`: **38 passed**, no skips. One upstream Starlette/AnyIO deprecation
  warning about `BlockingPortal`; it does not affect the test result.
- Shell syntax checks for `scripts/build-image` and `guest/init.sh`: passed.
- Source distribution and wheel build: passed, with no parent-repository runtime
  dependency. MCP tests exercised legacy 2025-11-25 and modern 2026-07-28 requests.
- Cross-workspace, expiration, revocation, rotation, retry conflicts, concurrent
  admission, busy suspension, uncertain outcomes and file traversal checks passed.

### Real Firecracker/KVM smoke

`scripts/smoke.py --config <temporary-config> --network` passed all 16 assertions:

1. Jailed Debian boot: 0.893 s in the final run.
2. Unprivileged guest Python, Node and GCC toolchains.
3. Cross-workspace execution refused.
4. File upload, C compilation, and generated executable output.
5. Network-off VM has only loopback.
6. Writes in one CoW disk do not appear in another VM.
7. Hibernation releases the VMM process; restore after reconstructing controller
   state took 0.044 s in the final run.
8. A Python process retains an in-memory value and completes after forced suspend.
9. Guest wall clock is corrected during restore.
10. Detached descendants remain active and are cancelled through their cgroup.
11. Immediate cancellation after dispatch kills the job.
12. Automatic idle hibernation and file-triggered wake.
13. Network-on VM reaches a public HTTPS endpoint.
14. Private and metadata HTTP connections fail.
15. Host nftables counters confirm the denied traffic hit the blocking rules.
16. Revoked workspace credential is rejected.

These are local functional smoke timings with a 256 MiB, one-vCPU guest, warm
storage/cache and a tmpfs-backed XFS loop device. They are not production latency,
NVMe hibernation throughput, density or cross-host portability measurements.

During development, the first run reproduced the upstream Firecracker 1.16 bare
pause/resume vsock defect. Upgrading to 1.17 resolved it; the service now refuses
older versions. See [upstream fix #6100](https://github.com/firecracker-microvm/firecracker/pull/6100).

### Actual fast-agent adapter

The generated manifest passed `fa-serve --check`. A temporary fa-serve instance
then booted `examples/tool_probe_agent.py` in its real jailed process-pool path.
Through `/v1/turn` and `host_fetch`, it created a Debian VM, ran Python yielding
`42`, retrieved output, hibernated, resumed and deleted the VM. The token was read
from the host's private secrets file; it was absent from the agent request and
response. No LLM or provider credential was required.

The fast-agent test used its developer `--jail-shared-uid --jail-no-cgroup` launch
mode; this does not qualify fast-agent's production jail configuration. The full
development microVMs themselves used the new service's distinct-UID Firecracker
jailer and cgroup limits. Parent Cargo workspace code was unchanged, and its full
test suite was not rerun.

The existing demo agent discovers tools from the manifest, so no demo source
patch was required. Shared multi-user demo identity routing remains intentionally
outside this first pass: a static token is one workspace shared by that instance.

Test VMs, service listeners, network namespaces, per-VM firewall rules and cgroups
were removed after validation. The prototype still needs a dedicated security
review and deployment hardening before exposure as a public multi-tenant service.
