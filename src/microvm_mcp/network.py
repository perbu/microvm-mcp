"""Per-VM namespaces. Only operator configuration selects the upstream interface."""

import ipaddress
import re
import subprocess
from urllib.parse import urlsplit


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


# Package managers the guest configures for network="packages".
MIRROR_KINDS = ("pypi", "npm")
SERVICE_RANGES = (ipaddress.ip_network("10.231.0.0/16"), ipaddress.ip_network("10.232.0.0/16"))


def guest_address(vm):
    return f"10.231.{vm['slot']}.2"


def gateway(vm):
    return f"10.231.{vm['slot']}.1"


def package_mirrors(config):
    """Validate the operator's [packages] table into {kind: {address, port, path}}.

    Only operator configuration names mirrors. The guest reaches each one at its own
    gateway on the mirror's port, so mirrors are plain HTTP to an IPv4 literal: no
    guest DNS is needed and no certificate could match the gateway address.
    """
    table = config.get("packages", {})
    if not isinstance(table, dict) or not set(table) <= set(MIRROR_KINDS):
        raise ValueError(f"[packages] accepts only {', '.join(MIRROR_KINDS)}")
    mirrors, ports = {}, {}
    for kind, url in sorted(table.items()):
        if not isinstance(url, str) or len(url) > 200:
            raise ValueError(f"packages.{kind} must be an http:// URL")
        parts = urlsplit(url)
        try:
            address = ipaddress.IPv4Address(parts.hostname or "")
            port = parts.port
        except ValueError:
            raise ValueError(f"packages.{kind} needs an IPv4 address and port") from None
        if parts.scheme != "http" or parts.username or parts.password:
            raise ValueError(f"packages.{kind} must be a plain http:// URL")
        if parts.query or parts.fragment or not port:
            raise ValueError(f"packages.{kind} needs an explicit port and no query")
        if (
            address.is_loopback
            or address.is_unspecified
            or address.is_multicast
            or any(address in r for r in SERVICE_RANGES)
        ):
            raise ValueError(f"packages.{kind} must be reachable from the VM namespaces")
        path = parts.path or "/"
        # Boot arguments and a guest shell profile carry this path.
        if not re.fullmatch(r"/[A-Za-z0-9._~/+-]*", path):
            raise ValueError(f"packages.{kind} path has unsupported characters")
        # The guest-facing port is the mirror port. One port, one destination.
        if ports.setdefault(port, address) != address:
            raise ValueError(f"packages mirrors on port {port} need the same address")
        mirrors[kind] = {"address": str(address), "port": port, "path": path}
    return mirrors


def mirror_targets(vm):
    return sorted({(m["address"], m["port"]) for m in vm.get("mirrors", {}).values()})


def mirror_urls(vm):
    """Guest-facing mirror URLs: each mirror's port and path at the VM gateway."""
    return {
        kind: f"http://{gateway(vm)}:{m['port']}{m['path']}"
        for kind, m in sorted(vm.get("mirrors", {}).items())
    }


def firewall(vm, upstream, extra_blocked=()):
    if vm["network"] == "packages":
        return packages_firewall(vm)
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", upstream or ""):
        raise ValueError("invalid upstream interface")
    _, host, table = names(vm)
    guest = guest_address(vm)
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


def packages_firewall(vm):
    """Host policy for network="packages": the guest reaches only its mirrors.

    Each mirror rule matches the guest source, exact address and TCP port. A rule
    in input covers a mirror on this host; the same rule in forward covers one
    elsewhere, which also needs masquerade. Everything else from the VM drops,
    including DNS. Explicit mirrors are exact exceptions to the blocked ranges.
    """
    _, host, table = names(vm)
    targets = mirror_targets(vm)
    if not targets:
        raise ValueError('network="packages" requires configured package mirrors')
    allow = [f"ip saddr {guest_address(vm)} ip daddr {a} tcp dport {p}" for a, p in targets]
    accept = "".join(f'\n        iifname "{host}" {rule} accept' for rule in allow)
    nat = "".join(f"\n        {rule} masquerade" for rule in allow)
    return f"""table inet {table} {{
      chain input {{ type filter hook input priority -10; policy accept;{accept}
        iifname "{host}" counter drop
      }}
      chain forward {{ type filter hook forward priority -10; policy accept;
        iifname "{host}" meta nfproto != ipv4 drop
        iifname "{host}" ip saddr != {guest_address(vm)} drop{accept}
        iifname "{host}" counter drop
        oifname "{host}" ct state established,related accept
        oifname "{host}" drop
      }}
      chain nat {{ type nat hook postrouting priority srcnat; policy accept;{nat}
      }}
    }}
    """


def namespace_firewall(vm):
    """Inside the VM's namespace: no inbound, and in packages mode DNAT each mirror
    port on the gateway to its mirror and forward nothing else."""
    rules = """
      table inet isolation {
        chain input { type filter hook input priority 0; policy drop;
          iifname "lo" accept
        }
      }
    """
    if vm["network"] != "packages":
        return rules
    dnat = "".join(
        f'\n          iifname "tap0" ip saddr {guest_address(vm)} ip daddr {gateway(vm)} '
        f"tcp dport {p} dnat to {a}:{p}"
        for a, p in mirror_targets(vm)
    )
    return (
        rules
        + f"""
      table ip packages {{
        chain prerouting {{ type nat hook prerouting priority dstnat; policy accept;{dnat}
        }}
        chain forward {{ type filter hook forward priority 0; policy drop;
          iifname "tap0" ct status dnat accept
          oifname "tap0" ct state established,related accept
        }}
      }}
    """
    )


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
        if vm["network"] == "on" and not self.upstream:
            raise ValueError("network=on requires an operator-configured uplink")
        slot = vm["slot"]
        # Install host filtering before making any guest path live.
        run("nft", "-f", "-", input=firewall(vm, self.upstream, self.blocked))
        # Existing Docker/UFW installations commonly have INPUT/FORWARD policy DROP.
        # Add only this VM's flows. The earlier nft chain still enforces all denies.
        for chain, rule in self.iptables_rules(vm):
            run("iptables", "-w", "5", "-I", chain, "1", *rule)
        run("ip", "netns", "exec", ns, "nft", "-f", "-", input=namespace_firewall(vm))
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

    def destroy(self, vm):
        ns, host, table = names(vm)
        run("ip", "link", "del", host, check=False)
        run("ip", "netns", "del", ns, check=False)
        for chain, rule in self.iptables_rules(vm):
            run("iptables", "-w", "5", "-D", chain, *rule, check=False)
        run("nft", "delete", "table", "inet", table, check=False)

    def iptables_rules(self, vm):
        """(chain, rule) pairs that only open this VM's allowed flows."""
        _, host, _ = names(vm)
        guest = guest_address(vm)
        established = ["-d", guest, "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED"]
        if vm["network"] == "packages":
            rules = [("FORWARD", ["-o", host, *established, "-j", "ACCEPT"])]
            for address, port in mirror_targets(vm):
                flow = ["-i", host, "-s", guest, "-d", address, "-p", "tcp"]
                flow += ["--dport", str(port), "-j", "ACCEPT"]
                rules += [("INPUT", flow), ("FORWARD", flow)]
            return rules
        if vm["network"] != "on" or not self.upstream:
            return []
        return [
            ("FORWARD", ["-i", host, "-o", self.upstream, "-s", guest, "-j", "ACCEPT"]),
            ("FORWARD", ["-o", host, "-i", self.upstream, *established, "-j", "ACCEPT"]),
        ]
