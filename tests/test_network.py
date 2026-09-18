from microvm_mcp.network import firewall, names


def test_network_policy_source_destination_interface_and_inbound():
    vm = {"id": "a" * 32, "slot": 3}
    rules = firewall(vm, "eth0", ["8.8.8.0/24"])
    assert "meta nfproto != ipv4 drop" in rules
    assert "ip saddr != 10.231.3.2 drop" in rules
    assert "169.254.0.0/16" in rules
    assert "100.64.0.0/10" in rules
    assert "8.8.8.0/24" in rules
    assert 'oifname != "eth0" drop' in rules
    assert f'iifname "{names(vm)[1]}" drop' in rules
    assert f'oifname "{names(vm)[1]}" drop' in rules
