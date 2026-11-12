import ipaddress
import socket

import pytest

from microvm_mcp.cli import listen_addresses, listen_sockets


def ipv6_loopback_available():
    try:
        with socket.socket(socket.AF_INET6) as sock:
            sock.bind(("::1", 0))
        return True
    except OSError:
        return False


def test_listen_defaults_to_ipv4_loopback():
    assert listen_addresses({}) == [ipaddress.ip_address("127.0.0.1")]


def test_listen_accepts_ipv4_and_ipv6_wildcards():
    assert [str(a) for a in listen_addresses({"listen": ["0.0.0.0", "::"]})] == ["0.0.0.0", "::"]


@pytest.mark.parametrize(
    "listen",
    [[], "0.0.0.0", ["localhost"], ["0.0.0.0:1050"], [1050], ["::", "::0"]],
)
def test_listen_rejects_malformed_values(listen):
    with pytest.raises(ValueError):
        listen_addresses({"listen": listen})


def test_listen_sockets_bind_one_port_per_family():
    if not ipv6_loopback_available():
        pytest.skip("host has no IPv6 loopback")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    sockets = listen_sockets({"port": port, "listen": ["127.0.0.1", "::1"]})
    try:
        v4, v6 = sockets
        assert v4.family == socket.AF_INET and v4.getsockname() == ("127.0.0.1", port)
        assert v6.family == socket.AF_INET6 and v6.getsockname()[:2] == ("::1", port)
        assert v6.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY) == 1
    finally:
        for sock in sockets:
            sock.close()


def test_listen_sockets_close_earlier_sockets_on_bind_failure():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with pytest.raises(OSError):
            listen_sockets({"port": port, "listen": ["127.0.0.2", "127.0.0.1"]})
        # The first socket was released: its address and port can be bound again.
        with socket.socket() as again:
            again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            again.bind(("127.0.0.2", port))
