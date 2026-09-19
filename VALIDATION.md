# Validation

## Restore-clock investigation and fix — 2026-09-19

The old failure was real clock loss, not just a tight assertion. A temporary
probe ran Python inside the unchanged guest, sampling nanosecond realtime and
monotonic clocks between host timestamps. Both `tsc` and `kvm-clock` were available.

| Guest clock / restore setting | Observed behavior |
| --- | --- |
| `tsc`, `clock_realtime=true` (old default) | A 3-second hibernation plus snapshot overhead left wall time 6.0–6.4 seconds behind. A subsequent 6-second hibernation increased the total lag to 16.8–17.2 seconds. |
| `kvm-clock`, `clock_realtime=true` | Wall time tracked the host, but monotonic time also advanced through the entire suspension. |
| `kvm-clock`, `clock_realtime=false` | Monotonic time excluded the hibernation interval, but wall time fell behind without separate correction. |
| Plain pause, either clocksource | Monotonic time advanced during the pause, consuming the old supervisor's command timeout budget. |

Firecracker's [snapshot documentation](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/docs/snapshotting/snapshot-support.md#loading-snapshots)
limits its automatic realtime advancement to `kvm-clock`. Its
[restore implementation](https://github.com/firecracker-microvm/firecracker/blob/v1.17.0/src/vmm/src/arch/x86_64/vm.rs)
passes `KVM_CLOCK_REALTIME` to KVM. Selecting a different clocksource alone
therefore did not meet both wall-time and managed-timeout requirements.

The earlier smoke also waited only 1.1 seconds while allowing two seconds of
clock error. With fast snapshot storage it could pass without any correction;
the 2026-09-18 pass was not evidence that long suspensions preserved wall time.

The fix freezes the managed guest cgroup before pausing the VMM, records its
monotonic suspension marker, and disables timeout checks while frozen. The host
disables Firecracker realtime advancement and sends current host wall time over
vsock at boot/resume. The supervisor sets only `CLOCK_REALTIME` before thawing,
and shifts running-job deadlines by the guest monotonic time spent frozen under
the same lock used for timeout checks. This handles both pause and hibernate,
including a paused VM subsequently checkpointed. Old guest images/checkpoints
without the new handshake are rejected; lifecycle failures stop the VMM.

The new smoke samples clocks directly through the supervisor and brackets the
sample with host nanosecond timestamps. It reports the measured skew interval
and clocksource, and enforces a 250 ms tolerance (previously two seconds with
integer rounding and command/polling overhead).

- Ruff lint/format checks and **75 unit tests passed**, no skips. Tests include
  timeout-budget accounting, retry behavior, clock-failure handling, freeze
  ordering, and rejecting old guests/checkpoints before use.
- Rebuilt rootfs SHA-256:
  `50b13f41721ab7455c1006aeb1168ae3751f80f8b99f58c02b52485d17ab7df3`.
  Same pinned kernel and Firecracker 1.17.0 as earlier, on XFS.
- All **28 real-KVM smoke checks passed in each of three runs**: default `tsc`
  with five-second suspension probes, `tsc` with 30-second probes, and
  `kvm-clock` (selected with the kernel's `clocksource=kvm-clock` boot argument)
  with five-second probes. Every run included networking. Three-second jobs
  survived pause, hibernate, and pause-to-hibernate,
  then completed with corrected wall time. Ordinary active timeouts still fired.
  Cancellation, automatic suspension/wake, network isolation, and credential
  revocation also passed. Across 12 clock samples, the complete measured skew
  intervals stayed within 1.4 ms of zero. The stricter 250 ms interval assertion
  was also checked against all captured samples.

These are local functional measurements, not a clock-accuracy SLA. Wall time
follows the host and is corrected at lifecycle boundaries, not continuously.
Applications' own timers retain guest kernel semantics; only managed command
timeout budgets are compensated. See README.md for those limits.

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
  were not reached in this run. The subsequent investigation above resolved it.
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
