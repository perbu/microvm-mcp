# Kubernetes deployment

These files run the service as a privileged pod on a bare-metal KVM node. The
pod uses the same Debian packages, preflight checks and security properties as
the systemd unit. It is still one service instance with node-local state.
Kubernetes schedules and restarts it; it does not make the service highly
available. Read the main README's host setup first; every requirement there still
applies. The node supplies them, and the pod's namespaces replace systemd.

| File | Purpose |
| --- | --- |
| `Dockerfile`, `build-container` | Image from the `.deb` files built by `scripts/build-debs` |
| `entrypoint.sh` | Cgroup delegation, pod IPv4 forwarding, image wait, then `microvm-mcp serve` |
| `manifests/namespace.yaml` | Namespace with the privileged Pod Security level |
| `manifests/storage.yaml` | Local StorageClass and one PersistentVolume per node |
| `manifests/configmap.yaml` | `service.toml` and the TLS proxy's Caddyfile |
| `manifests/statefulset.yaml` | Service container plus TLS proxy sidecar |
| `manifests/service.yaml` | ClusterIP Service on port 443 |
| `manifests/networkpolicy.yaml` | Ingress from clients only; public IPv4 egress for guests |

## Design

- **One pod per KVM node, never moved.** Hibernated VMs restore only on the same
  host with the same Firecracker binary. State lives on a local PersistentVolume
  pinned to the node. More capacity means more StatefulSets, each with its own
  node, volume and workspace tokens. Clients must reach the instance that issued
  their workspace's token.
- **Privileged service container.** Jailer needs mknod, chroot, setuid, mount
  and network namespaces, and access to `/dev/kvm` and `/dev/net/tun`. The pod
  mounts no ServiceAccount token and has no host network, PID or IPC namespaces.
- **Cgroups.** With a private cgroup namespace, jailer creates
  `microvm-mcp/<vm>` below the container's cgroup, so VM memory is charged to the
  pod. The entrypoint moves the container's own processes into an `init` leaf
  and delegates `cpu`, `memory` and `pids`. Without this, enabling controllers in
  a cgroup that holds processes fails. It refuses to start if the runtime gives
  it the node's cgroup root.
- **Networking in the pod's namespace.** `uplink = "eth0"` is the pod's
  interface. Per-VM network namespaces, veths, nftables tables and iptables
  FORWARD rules live in the pod's network namespace, away from kube-proxy and the
  CNI. The entrypoint enables `net.ipv4.ip_forward` only there. Guest traffic is
  masqueraded to the pod IP, so NetworkPolicy egress rules apply to it as well.
- **Loopback listener plus TLS sidecar.** The service still binds `127.0.0.1`.
  A Caddy sidecar terminates TLS on `:8443` and preserves the client's `Host`
  header, which must appear in `allowed_hosts`.
- **Graceful shutdown.** tini signals only the controller, which checkpoints
  running VMs. `terminationGracePeriodSeconds: 900` mirrors the unit's
  `TimeoutStopSec`. There is deliberately no liveness probe: a restart kills
  running guests instead of checkpointing them.

## Node preparation

On each KVM node:

1. Enable VT-x/AMD-V in firmware. Check that `/dev/kvm` exists and the node uses
   cgroup v2: `stat -fc %T /sys/fs/cgroup` prints `cgroup2fs`.
2. Load the modules the pod cannot load itself, at boot:

   ```sh
   printf '%s\n' kvm_intel tun veth nf_tables nft_masq nft_ct nf_nat \
     ip_tables iptable_filter | sudo tee /etc/modules-load.d/microvm-mcp.conf
   sudo systemctl restart systemd-modules-load
   ```

   Use `kvm_amd` on AMD hosts.
3. Create the dedicated reflink-capable filesystem and mount it persistently,
   without `noexec` or `nodev`:

   ```sh
   sudo mkfs.xfs -m reflink=1 /dev/DEVICE
   sudo mkdir -p /srv/microvm-mcp
   echo '/dev/DEVICE /srv/microvm-mcp xfs defaults 0 2' | sudo tee -a /etc/fstab
   sudo mount /srv/microvm-mcp && sudo chmod 0700 /srv/microvm-mcp
   ```

   Size it for every VM's fully written disk plus two RAM checkpoint generations.
4. Make sure no account or other workload on the node uses the jail UID range
   (`uid_base` through `uid_base + max_vms - 1`). Without user namespaces these
   are real node UIDs. Preflight only sees the container's `/etc/passwd`.
5. Label and taint the node so only this workload lands there:

   ```sh
   kubectl label node kvm-node-1 microvm-mcp/kvm=true
   kubectl taint node kvm-node-1 microvm-mcp/dedicated=true:NoSchedule
   ```

The container runtime must give containers a private cgroup namespace on cgroup
v2. containerd and CRI-O do by default.

## Build the image

```sh
scripts/build-debs
deploy/k8s/build-container registry.example.com/microvm-mcp:0.1.0-1
docker push registry.example.com/microvm-mcp:0.1.0-1
```

The build verifies the packages against `SHA256SUMS`. Keep the Firecracker
package byte-identical across image upgrades. Checkpoints made by one binary are
rejected by another.

## Configure and deploy

Edit every `REPLACE` marker in `manifests/`:

- `storage.yaml`: node name, path and capacity.
- `configmap.yaml`: digests, quotas, `blocked_cidrs` and `allowed_hosts`.
- `statefulset.yaml`: image reference, resource sizing and volume size.
- `networkpolicy.yaml`: which clients may connect.

Add any node, BMC/management, pod or service CIDRs that are outside the private
ranges to both `blocked_cidrs` and the NetworkPolicy `except` list. Keep the
memory request and limit at least `max_memory_mib + 256 MiB * max_vms`, plus
headroom for the service and page cache.

Create the TLS secret, for example from cert-manager or an existing certificate
whose names match `allowed_hosts`:

```sh
kubectl apply -f deploy/k8s/manifests/namespace.yaml
kubectl -n microvm-mcp create secret tls microvm-mcp-tls --cert=tls.crt --key=tls.key
kubectl apply -k deploy/k8s/manifests
```

## Install the guest image

The pod starts and waits until the configured rootfs exists. Copy in the
independently verified kernel and rootfs, then install them onto the state
volume:

```sh
kubectl -n microvm-mcp exec microvm-mcp-0 -c microvm-mcp -- \
  install -d -m 0700 /var/lib/microvm-mcp/incoming
kubectl -n microvm-mcp cp debian-13.ext4 microvm-mcp-0:/var/lib/microvm-mcp/incoming/ -c microvm-mcp
kubectl -n microvm-mcp cp vmlinux microvm-mcp-0:/var/lib/microvm-mcp/incoming/ -c microvm-mcp
kubectl -n microvm-mcp exec microvm-mcp-0 -c microvm-mcp -- microvm-mcp-install-image \
  --state-dir /var/lib/microvm-mcp --image-id debian-13-dev-v1 \
  --rootfs /var/lib/microvm-mcp/incoming/debian-13.ext4 --rootfs-sha256 VERIFIED_ROOTFS_SHA256 \
  --kernel /var/lib/microvm-mcp/incoming/vmlinux --kernel-sha256 VERIFIED_KERNEL_SHA256
kubectl -n microvm-mcp exec microvm-mcp-0 -c microvm-mcp -- rm -r /var/lib/microvm-mcp/incoming
```

You can also copy the files directly into `/srv/microvm-mcp/incoming` on the
node. Within 30 seconds the entrypoint starts the service, which runs preflight
and becomes ready. A preflight failure exits the container. Read
`kubectl logs microvm-mcp-0 -c microvm-mcp`.

## Workspace tokens

Issuance stays a local administrator action. Write the token inside the pod, move
it straight into a Secret for the trusted client or broker, then delete it:

```sh
kubectl -n microvm-mcp exec microvm-mcp-0 -c microvm-mcp -- microvm-mcp issue-token \
  --state-dir /var/lib/microvm-mcp --workspace demo-alice \
  --output /run/demo-alice.token --ttl 86400 --allow-network
kubectl -n microvm-mcp exec microvm-mcp-0 -c microvm-mcp -- cat /run/demo-alice.token \
  | kubectl -n microvm-clients create secret generic microvm-demo-alice --from-file=token=/dev/stdin
kubectl -n microvm-mcp exec microvm-mcp-0 -c microvm-mcp -- rm /run/demo-alice.token
```

Never mount a token into the microvm-mcp pod's configuration or pass one to a
guest. Revocation works the same way through `microvm-mcp revoke-workspace`.

## Operations

- **Maintenance mode.** Set `MICROVM_MAINTENANCE=1` to restart the pod with the
  volume, namespaces and cgroups prepared but the service not running. It
  releases the state lock for `scripts/smoke.py` and offline inspection.
  Running VMs are checkpointed on the way down.
- **Validation.** In maintenance mode, copy `scripts/smoke.py` into the pod and
  run the installed-package smoke command from `packaging/debian/README.md` with
  `--config /etc/microvm-mcp/service.toml --network --clock-suspend-seconds 30`.
  Also restart the pod with running and paused VMs, and confirm they resume.
  Package checks and these manifests have not been qualified on a real cluster
  until that passes.
- **Node drain** evicts the pod, checkpointing its VMs. They resume only when
  the pod returns to the same node. There is no migration, and a
  PodDisruptionBudget cannot help a single replica.
- **Forced termination**, OOM kills and node failures leave running VMs marked
  failed, as after a service crash. Hibernated VMs remain restorable. Keep the
  node dedicated (taint, resource requests) to avoid pressure evictions.
- **MTU.** Guest TAP devices use 1500 bytes. On overlay CNIs with a smaller pod
  MTU, path MTU discovery relies on ICMP "fragmentation needed" messages
  reaching the guest. The per-VM rules accept related traffic.
- **Upgrades** follow the Debian upgrade rules: same Firecracker binary for
  existing checkpoints, and new guest images need a new image ID, digest and
  new VMs.
