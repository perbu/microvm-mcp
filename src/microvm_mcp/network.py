"""Per-VM namespaces. Only operator configuration selects the upstream interface."""

import ipaddress
import re
import subprocess


def run(*args, input=None, check=True):
    return subprocess.run(
        args, input=input, text=True, check=check, capture_output=True, timeout=15
    )


def names(vm):
    suffix = vm["id"][:10]
    return f"mvm-{suffix}", f"mvh{suffix}", f"mvm_{suffix}"


BLOCKED = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.0.2.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "198.51.100.0/24",
    "203.0.113.0/24",
    "224.0.0.0/4",
    "240.0.0.0/4",
)


def firewall(vm, upstream, extra_blocked=()):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", upstream):
        raise ValueError("invalid upstream interface")
    _, host, table = names(vm)
    guest = f"10.231.{vm['slot']}.2"
    blocked = ", ".join(str(ipaddress.ip_network(n)) for n in (*BLOCKED, *extra_blocked))
    # Accept does not override other host base chains. Drops are final. All traffic
    # from the VM is IPv4, source pinned, and must leave by the configured uplink.
    return f"""table inet {table} {{
      chain input {{ type filter hook input priority -10; policy accept;
        iifname "{host}" drop
      }}
      chain forward {{ type filter hook forward priority -10; policy accept;
        iifname "{host}" meta nfproto != ipv4 drop
        iifname "{host}" ip saddr != {guest} drop
        iifname "{host}" ip daddr {{ {blocked} }} counter drop
        iifname "{host}" oifname != "{upstream}" drop
        oifname "{host}" ct state established,related accept
        oifname "{host}" drop
      }}
      chain nat {{ type nat hook postrouting priority srcnat; policy accept;
        ip saddr {guest} oifname "{upstream}" masquerade
      }}
    }}
    """


class Network:
    def __init__(self, upstream=None, blocked=(), uid_base=62000):
        self.upstream, self.blocked = upstream, blocked
        self.uid_base = uid_base

    def create(self, vm):
        ns, host, _ = names(vm)
        run("ip", "netns", "add", ns)
        try:
            self._create(vm, ns, host)
        except Exception:
            self.destroy(vm)
            raise

    def _create(self, vm, ns, host):
        run("ip", "-n", ns, "link", "set", "lo", "up")
        if vm["network"] == "off":
            return
        if not self.upstream:
            raise ValueError("network=on requires an operator-configured uplink")
        slot = vm["slot"]
        # Install host filtering before making any guest path live.
        run("nft", "-f", "-", input=firewall(vm, self.upstream, self.blocked))
        # Existing Docker/UFW installations commonly have FORWARD policy DROP.
        # Add only this VM's flows. The earlier nft chain still enforces all denies.
        for rule in self.forward_rules(vm):
            run("iptables", "-w", "5", "-I", "FORWARD", "1", *rule)
        run("ip", "link", "add", host, "type", "veth", "peer", "name", "uplink", "netns", ns)
        run("ip", "addr", "add", f"10.232.{slot}.1/30", "dev", host)
        run("ip", "-n", ns, "addr", "add", f"10.232.{slot}.2/30", "dev", "uplink")
        run("ip", "link", "set", host, "up")
        run("ip", "-n", ns, "link", "set", "uplink", "up")
        run(
            "ip",
            "netns",
            "exec",
            ns,
            "ip",
            "tuntap",
            "add",
            "dev",
            "tap0",
            "mode",
            "tap",
            "user",
            str(self.uid_base + slot),
        )
        run("ip", "-n", ns, "addr", "add", f"10.231.{slot}.1/30", "dev", "tap0")
        run("ip", "-n", ns, "link", "set", "tap0", "up")
        run("ip", "-n", ns, "route", "add", "default", "via", f"10.232.{slot}.1")
        run("ip", "route", "add", f"10.231.{slot}.0/30", "via", f"10.232.{slot}.2", "dev", host)
        run("ip", "netns", "exec", ns, "sysctl", "-qw", "net.ipv4.ip_forward=1")
        run(
            "ip",
            "netns",
            "exec",
            ns,
            "nft",
            "-f",
            "-",
            input="""
          table inet isolation {
            chain input { type filter hook input priority 0; policy drop;
              iifname "lo" accept
            }
          }
        """,
        )

    def destroy(self, vm):
        ns, host, table = names(vm)
        run("ip", "link", "del", host, check=False)
        run("ip", "netns", "del", ns, check=False)
        if vm["network"] == "on" and self.upstream:
            for rule in self.forward_rules(vm):
                run("iptables", "-w", "5", "-D", "FORWARD", *rule, check=False)
        run("nft", "delete", "table", "inet", table, check=False)

    def forward_rules(self, vm):
        _, host, _ = names(vm)
        guest = f"10.231.{vm['slot']}.2"
        return [
            ["-i", host, "-o", self.upstream, "-s", guest, "-j", "ACCEPT"],
            [
                "-o",
                host,
                "-i",
                self.upstream,
                "-d",
                guest,
                "-m",
                "conntrack",
                "--ctstate",
                "ESTABLISHED,RELATED",
                "-j",
                "ACCEPT",
            ],
        ]
