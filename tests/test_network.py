import pytest

from microvm_mcp.network import (
    Network,
    firewall,
    mirror_urls,
    names,
    namespace_firewall,
    package_mirrors,
)

MIRRORS = {
    "pypi": "http://192.168.10.5:3141/root/pypi/+simple/",
    "npm": "http://192.168.10.5:4873/",
}


def packages_vm(slot=3, table=MIRRORS):
    return {
        "id": "a" * 32,
        "slot": slot,
        "network": "packages",
        "mirrors": package_mirrors({"packages": table}),
    }


def test_network_policy_source_destination_interface_and_inbound():
    vm = {"id": "a" * 32, "slot": 3, "network": "on"}
    rules = firewall(vm, "eth0", ["8.8.8.0/24"])
    assert "meta nfproto != ipv4 drop" in rules
    assert "ip saddr != 10.231.3.2 drop" in rules
    assert "169.254.0.0/16" in rules
    assert "100.64.0.0/10" in rules
    assert "8.8.8.0/24" in rules
    assert 'oifname != "eth0" drop' in rules
    assert f'iifname "{names(vm)[1]}" drop' in rules
    assert f'oifname "{names(vm)[1]}" drop' in rules


def test_package_mirrors_parse_operator_urls():
    assert package_mirrors({}) == {}
    assert package_mirrors({"packages": MIRRORS}) == {
        "npm": {"address": "192.168.10.5", "port": 4873, "path": "/"},
        "pypi": {"address": "192.168.10.5", "port": 3141, "path": "/root/pypi/+simple/"},
    }
    # One repository manager serving both on one port is fine.
    shared = package_mirrors(
        {"packages": {"pypi": "http://10.0.0.9:8081/py/simple/", "npm": "http://10.0.0.9:8081/js/"}}
    )
    assert {m["port"] for m in shared.values()} == {8081}


@pytest.mark.parametrize(
    "table",
    [
        {"cargo": "http://10.0.0.9:80/"},
        {"pypi": "https://10.0.0.9:443/simple/"},
        {"pypi": "http://mirror.example:3141/simple/"},
        {"pypi": "http://10.0.0.9/simple/"},
        {"pypi": "http://10.0.0.9:0/simple/"},
        {"pypi": "http://10.0.0.9:3141/simple/?x=1"},
        {"pypi": "http://user:pw@10.0.0.9:3141/simple/"},
        {"pypi": "http://127.0.0.1:3141/simple/"},
        {"pypi": "http://0.0.0.0:3141/simple/"},
        {"pypi": "http://10.231.0.1:3141/simple/"},
        {"pypi": "http://10.232.4.1:3141/simple/"},
        {"pypi": "http://[::1]:3141/simple/"},
        {"pypi": "http://10.0.0.9:3141/simple/ init=/bin/sh"},
        {"pypi": "http://10.0.0.9:3141/simple/'$(id)'"},
        {"pypi": "http://10.0.0.9:3141/a/", "npm": "http://10.0.0.8:3141/b/"},
        {"pypi": 3141},
        ["http://10.0.0.9:3141/"],
    ],
)
def test_package_mirrors_reject_unsafe_config(table):
    with pytest.raises(ValueError):
        package_mirrors({"packages": table})


def test_mirror_urls_use_the_vm_gateway():
    assert mirror_urls(packages_vm(slot=7)) == {
        "npm": "http://10.231.7.1:4873/",
        "pypi": "http://10.231.7.1:3141/root/pypi/+simple/",
    }
    assert mirror_urls({"id": "a" * 32, "slot": 7, "network": "on"}) == {}


def test_packages_policy_allows_only_mirror_flows():
    vm = packages_vm()
    host = names(vm)[1]
    rules = firewall(vm, None, ["8.8.8.0/24"])
    for port in (3141, 4873):
        flow = f"ip saddr 10.231.3.2 ip daddr 192.168.10.5 tcp dport {port}"
        # Local mirror (input), remote mirror (forward), and its return path (masquerade).
        assert rules.count(f'iifname "{host}" {flow} accept') == 2
        assert f"{flow} masquerade" in rules
    assert rules.count(f'iifname "{host}" counter drop') == 2
    assert "ip saddr != 10.231.3.2 drop" in rules
    assert "meta nfproto != ipv4 drop" in rules
    assert f'oifname "{host}" drop' in rules
    # No general egress: no uplink accept, no unconditional masquerade.
    assert "oifname !=" not in rules
    assert "ip saddr 10.231.3.2 oifname" not in rules


def test_packages_policy_requires_mirrors():
    vm = {"id": "a" * 32, "slot": 3, "network": "packages", "mirrors": {}}
    with pytest.raises(ValueError):
        firewall(vm, "eth0")


def test_namespace_dnat_maps_gateway_ports_to_mirrors():
    rules = namespace_firewall(packages_vm())
    assert "policy drop" in rules
    for port in (3141, 4873):
        assert (
            f"ip saddr 10.231.3.2 ip daddr 10.231.3.1 tcp dport {port} dnat to 192.168.10.5:{port}"
        ) in rules
    assert 'iifname "tap0" ct status dnat accept' in rules
    on = namespace_firewall({"id": "a" * 32, "slot": 3, "network": "on"})
    assert "dnat" not in on and "isolation" in on


def test_iptables_rules_open_only_this_vms_flows():
    vm = packages_vm()
    host = names(vm)[1]
    rules = Network("eth0").iptables_rules(vm)
    chains = [chain for chain, _ in rules]
    assert chains.count("INPUT") == 2 and chains.count("FORWARD") == 3
    for _, rule in rules:
        assert host in rule and "10.231.3.2" in rule
        assert "eth0" not in rule
    accepted = {
        (r[r.index("-d") + 1], r[r.index("--dport") + 1]) for _, r in rules if "--dport" in r
    }
    assert accepted == {("192.168.10.5", "3141"), ("192.168.10.5", "4873")}
    on = Network("eth0").iptables_rules({**vm, "network": "on"})
    assert [c for c, _ in on] == ["FORWARD", "FORWARD"]
    assert Network(None).iptables_rules({**vm, "network": "on"}) == []
    assert Network("eth0").iptables_rules({**vm, "network": "off"}) == []
