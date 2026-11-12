#!/bin/sh
set -eu
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/pts /run /sys/fs/cgroup
mount -t devpts devpts /dev/pts
mount -t tmpfs -o size=32m tmpfs /run
mount -t cgroup2 none /sys/fs/cgroup
# Move PID 1 into a leaf before enabling controllers in the root.
mkdir /sys/fs/cgroup/init
echo $$ > /sys/fs/cgroup/init/cgroup.procs
echo +pids > /sys/fs/cgroup/cgroup.subtree_control
ip link set lo up
guest_ip=""
gateway=""
pypi=""
npm=""
for arg in $(cat /proc/cmdline); do
    case "$arg" in
        mvm_ip=*) guest_ip=${arg#mvm_ip=} ;;
        mvm_gateway=*) gateway=${arg#mvm_gateway=} ;;
        mvm_pypi=*) pypi=${arg#mvm_pypi=} ;;
        mvm_npm=*) npm=${arg#mvm_npm=} ;;
    esac
done
if [ -n "$guest_ip" ] && [ -n "$gateway" ]; then
    ip addr add "$guest_ip/30" dev eth0
    ip link set eth0 up
    ip route add default via "$gateway"
fi
if [ -n "$pypi$npm" ]; then
    # network=packages: the host allows only mirrors at the gateway, and no DNS.
    # An empty resolv.conf fails lookups at once instead of timing out.
    : > /etc/resolv.conf
    profile=/etc/profile.d/microvm-packages.sh
    : > "$profile"
    if [ -n "$pypi" ]; then
        printf "export PIP_INDEX_URL='%s'\nexport PIP_TRUSTED_HOST='%s'\n" \
            "$pypi" "$gateway" >> "$profile"
        printf '[global]\nindex-url = %s\ntrusted-host = %s\n' "$pypi" "$gateway" \
            > /etc/pip.conf
    fi
    if [ -n "$npm" ]; then
        printf "export NPM_CONFIG_REGISTRY='%s'\n" "$npm" >> "$profile"
    fi
else
    echo nameserver 1.1.1.1 > /etc/resolv.conf
fi
exec /usr/bin/python3 /opt/microvm/supervisor.py
