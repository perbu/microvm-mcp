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
for arg in $(cat /proc/cmdline); do
    case "$arg" in
        mvm_ip=*) guest_ip=${arg#mvm_ip=} ;;
        mvm_gateway=*) gateway=${arg#mvm_gateway=} ;;
    esac
done
if [ -n "$guest_ip" ] && [ -n "$gateway" ]; then
    ip addr add "$guest_ip/30" dev eth0
    ip link set eth0 up
    ip route add default via "$gateway"
fi
echo nameserver 1.1.1.1 > /etc/resolv.conf
exec /usr/bin/python3 /opt/microvm/supervisor.py
