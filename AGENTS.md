# Standalone project

This directory must remain extractable as an independent project. Do not add
imports or build dependencies on the parent repository. Keep optional integrations
as configuration generators or separate clients of the public HTTP/MCP API.

Read README.md before changing lifecycle, authorization, networking or storage.
Run the Python lint/format checks and pytest. Changes to the actual VM backend
also require scripts/smoke.py on a Linux KVM host; missing prerequisites fail.
Never introduce a host-subprocess backend that executes model-supplied commands.
Guest supervisor code executes only inside the full Linux microVM.

The owner comes only from authenticated workspace credentials. VM IDs, MCP
sessions, and tool arguments never carry ownership authority. Keep credentials
out of both the persistent Agent VM and the development guest.

## Command output

Keep stdout and stderr separate. The HTTP/MCP default is readable UTF-8 text;
base64 is an explicit option for exact binary output. Preserve whitespace and
tracebacks, flag replacement of invalid UTF-8, and never lose a character split
across reads. Output cursors are independent byte offsets for each stream.
Keep the shared output retention bound and drain both pipes even after it fills.

The catalog in src/microvm_mcp/catalog.py is shared by HTTP validation, MCP
discovery, and generated adapters. Update README.md and relevant clients when
changing the tool contract. Changes to guest code require rebuilding the image
and pinning its digest before real-VM validation; old VMs and checkpoints do not
automatically pick up supervisor changes.

## Images and streams

Only operator configuration names bootable images; tokens may narrow them, tool
arguments only choose among them. VMs keep private kernel/disk copies, so resume
must never depend on an image still being configured. Guest streams are host to
guest only, refuse ports 1023 and 1024, close on any suspension, and log
metadata only, never payload. Port 1023 is the job broker pipe: the token is
hashed in memory only and is not a guest, receipt, or log field. Firecracker's
vsock bridge has no host half-close; the smoke test asserts this, so revisit
the EOF contract if it starts failing.

## Suspension and clocks

Freeze the guest jobs cgroup before pausing vCPUs. Correct guest CLOCK_REALTIME
from the host before thawing jobs, and restore managed timeout budgets under
the supervisor lock before timeout checks resume. Never
enable Firecracker clock_realtime as a substitute: its kvm-clock path also
advances monotonic timers. Keep lifecycle protocol checks on new guests and
restored checkpoints. Test both clock alignment and jobs suspended longer than
their timeout, including pause-to-hibernate transitions. Preserve the distinction
between managed timeout accounting and applications' own kernel timers.

## Validation and licensing

Run the checks documented in README.md. Output changes need tests for stream
separation, pagination, split and invalid UTF-8, binary retrieval, and truncation.
Pure guest helpers may be unit-tested on the host, but guest execution must only
be exercised inside a full Linux microVM. Report missing prerequisites and smoke
failures explicitly; do not count skipped or unexecuted checks as passes.

This project is Apache-2.0 licensed. Keep LICENSE and Python package license
metadata consistent; preserve third-party notices when adding vendored code.
