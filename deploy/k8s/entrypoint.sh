#!/bin/sh
# Prepares the pod's cgroup and network namespaces, then replaces itself with the
# service so that tini's only child is the controller that checkpoints on SIGTERM.
set -eu

config=${MICROVM_CONFIG:-/etc/microvm-mcp/service.toml}
python=/usr/lib/microvm-mcp/python/bin/python3.13

die() {
    echo "microvm-mcp-entrypoint: $*" >&2
    exit 1
}

[ -f "$config" ] || die "missing configuration: $config"

# Jailer creates microvm-mcp/<vm> below the cgroup root it sees. Require a private
# cgroup namespace so VMs are charged to this pod, never to the node's root.
[ -f /sys/fs/cgroup/cgroup.controllers ] || die "cgroup v2 is required"
[ "$(cat /proc/self/cgroup)" = "0::/" ] || die "a private cgroup namespace is required"

# A cgroup holding processes cannot delegate domain controllers to children.
# Move this container's processes into a leaf, then enable what jailer sets.
# runc places later exec sessions (kubectl exec, probes) in the init process's
# cgroup when the container root refuses them.
mkdir -p /sys/fs/cgroup/init
while read -r pid; do
    echo "$pid" > /sys/fs/cgroup/init/cgroup.procs 2>/dev/null || :
done < /sys/fs/cgroup/cgroup.procs
for controller in cpu memory pids; do
    grep -qw "$controller" /sys/fs/cgroup/cgroup.controllers \
        || die "cgroup controller not delegated to the container: $controller"
    echo "+$controller" > /sys/fs/cgroup/cgroup.subtree_control
done

# net.ipv4.ip_forward is per network namespace: this affects only the pod.
if [ "${MICROVM_IP_FORWARD:-0}" = 1 ]; then
    echo 1 > /proc/sys/net/ipv4/ip_forward
fi

setting() {
    "$python" -I -B -c '
import sys, tomllib
with open(sys.argv[1], "rb") as source:
    print(tomllib.load(source)[sys.argv[2]])
' "$config" "$1"
}
state_dir=$(setting state_dir)
rootfs=$(setting rootfs)
[ -d "$state_dir" ] || die "state volume is not mounted: $state_dir"
chmod 0700 "$state_dir"

# Maintenance keeps the volume and namespaces available to kubectl exec without
# holding the service lock: image installation, smoke tests, inspection.
if [ "${MICROVM_MAINTENANCE:-0}" = 1 ]; then
    echo "microvm-mcp-entrypoint: maintenance mode; service not started" >&2
    exec sleep infinity
fi
while [ ! -f "$rootfs" ]; do
    echo "microvm-mcp-entrypoint: waiting for guest image $rootfs" \
        "(install it with microvm-mcp-install-image)" >&2
    sleep 30
done

exec /usr/bin/microvm-mcp serve --config "$config"
