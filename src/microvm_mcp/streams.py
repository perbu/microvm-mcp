"""Host-to-guest byte streams: policy and accounting, never payload."""

import socket
import time

import anyio

CONTROL_PORT = 1024  # The guest supervisor's RPC channel; never a stream target.
MAX_PORT = 2**32 - 2  # VMADDR_PORT_ANY is reserved.
CHUNK = 64 * 1024


def port_ranges(value):
    """Validate operator-configured [[low, high], ...] inclusive guest vsock port ranges."""
    ranges = []
    for item in value:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or not all(type(v) is int for v in item)
        ):
            raise ValueError("stream_ports entries must be [low, high] integer pairs")
        low, high = item
        if not 1 <= low <= high <= MAX_PORT:
            raise ValueError(f"invalid stream port range: {low}-{high}")
        if low <= CONTROL_PORT <= high:
            raise ValueError("stream_ports must exclude the guest control port 1024")
        ranges.append((low, high))
    return tuple(ranges)


class Stream:
    """One open stream. Closing it from another thread only shuts down its socket."""

    def __init__(self, vm_id, owner, port):
        self.vm_id, self.owner, self.port = vm_id, owner, port
        self.sock = None
        self.opened = time.monotonic()
        self.to_guest = self.from_guest = 0
        self.reason = None

    def close(self, reason):
        if self.reason is None:
            self.reason = reason
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class Rate:
    """Token bucket in bytes per second, with at most one second of burst."""

    def __init__(self, rate):
        self.rate = rate
        self.allowance = rate
        self.last = time.monotonic()

    async def take(self, size):
        if not self.rate:
            return
        now = time.monotonic()
        self.allowance = min(self.rate, self.allowance + (now - self.last) * self.rate)
        self.last = now
        self.allowance -= size
        if self.allowance < 0:
            await anyio.sleep(-self.allowance / self.rate)
