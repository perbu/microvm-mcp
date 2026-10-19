"""Job-scoped guest-to-host broker pipe. Copy bytes; never interpret or log them."""

import queue
import socket
import struct
import threading
import time
from pathlib import Path

from .errors import ServiceError

OPEN, DATA, CLOSE = 1, 2, 3
MAX_PAYLOAD = 65536
MAX_QUEUED = 32
MAX_CONNECTIONS = 64
# Guest-to-host preamble is the 32-hex job id and a newline, then raw bytes.
PREAMBLE = 33


class ProtocolError(Exception):
    pass


def encode(kind, conn_id, payload=b""):
    if kind == DATA:
        return struct.pack(">BII", DATA, conn_id, len(payload)) + payload
    return struct.pack(">BI", kind, conn_id)


class Parser:
    """Byte stream of multiplex frames. A websocket message may split or combine them."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        frames = []
        while True:
            if len(self.buf) < 5:
                break
            kind, conn_id = struct.unpack_from(">BI", self.buf)
            if conn_id == 0 or kind not in (OPEN, DATA, CLOSE):
                raise ProtocolError
            if kind != DATA:
                del self.buf[:5]
                frames.append((kind, conn_id, b""))
                continue
            if len(self.buf) < 9:
                break
            (length,) = struct.unpack_from(">I", self.buf, 5)
            if length > MAX_PAYLOAD:
                raise ProtocolError
            total = 9 + length
            if len(self.buf) < total:
                break
            payload = bytes(self.buf[9:total])
            del self.buf[:total]
            frames.append((DATA, conn_id, payload))
        return frames


class BrokerPipe:
    """One controller attachment for one job. Connection ids are assigned here."""

    def __init__(self, vm_id, owner, job_id, token_hash):
        self.vm_id, self.owner, self.job_id = vm_id, owner, job_id
        self.token_hash = token_hash
        self.attached = False
        self.reason = None
        self.conns = {}
        self.pending = []
        self.next_id = 1
        self.lock = threading.Lock()
        self.outbound = None
        self.to_guest = 0
        self.from_guest = 0
        self.opened_at = None
        self.logged = False

    def add_guest(self, sock):
        with self.lock:
            if self.reason == "destroyed":
                sock.close()
                return
            if not self.attached:
                if len(self.pending) >= MAX_QUEUED:
                    sock.close()
                    return
                self.pending.append(sock)
                return
            if len(self.conns) >= MAX_CONNECTIONS:
                sock.close()
                return
            conn_id = self._take_id()
            self.conns[conn_id] = sock
        self._start(conn_id, sock)

    def attach(self):
        with self.lock:
            if self.attached:
                raise ServiceError("conflict", "Broker pipe is already attached", 409)
            self.attached = True
            self.reason = None
            self.logged = False
            self.opened_at = time.monotonic()
            self.outbound = queue.Queue()
            pending = self.pending
            self.pending = []
        for sock in pending:
            with self.lock:
                if not self.attached or len(self.conns) >= MAX_CONNECTIONS:
                    sock.close()
                    continue
                conn_id = self._take_id()
                self.conns[conn_id] = sock
            self._start(conn_id, sock)

    def from_controller(self, kind, conn_id, payload):
        """Data and close from the controller. The service assigns open ids."""
        if kind == OPEN:
            raise ProtocolError
        with self.lock:
            sock = self.conns.get(conn_id)
            if sock is None:
                # The guest may already have closed this id.
                if kind == CLOSE:
                    return
                raise ProtocolError
            if kind == CLOSE:
                self.conns.pop(conn_id, None)
            elif kind == DATA:
                self.to_guest += len(payload)
            else:
                raise ProtocolError
        if kind == CLOSE:
            self._shutdown(sock)
            return
        sock.sendall(payload)

    def detach(self, reason):
        """Drop the controller and any guest sockets. The token hash stays."""
        with self.lock:
            if self.reason is None:
                self.reason = reason
            # Destroy sticks: a later detach must not look like a fresh pipe.
            if reason == "destroyed":
                self.reason = "destroyed"
            self.attached = False
            outbound = self.outbound
            socks = [*self.conns.values(), *self.pending]
            self.conns.clear()
            self.pending.clear()
            self.outbound = None
        for sock in socks:
            self._shutdown(sock)
        if outbound is not None:
            outbound.put(None)

    def _take_id(self):
        conn_id = self.next_id
        self.next_id = conn_id + 1 if conn_id < 2**32 - 1 else 1
        return conn_id

    def _start(self, conn_id, sock):
        with self.lock:
            outbound = self.outbound
            live = outbound is not None and conn_id in self.conns
        if not live:
            self._shutdown(sock)
            return
        outbound.put(encode(OPEN, conn_id))
        threading.Thread(
            target=self._read_guest, args=(conn_id, sock, outbound), daemon=True
        ).start()

    def _read_guest(self, conn_id, sock, outbound):
        try:
            while True:
                data = sock.recv(MAX_PAYLOAD)
                if not data:
                    break
                with self.lock:
                    if conn_id not in self.conns or self.outbound is not outbound:
                        return
                    self.from_guest += len(data)
                outbound.put(encode(DATA, conn_id, data))
        except OSError:
            pass
        with self.lock:
            removed = self.conns.pop(conn_id, None) is not None and self.outbound is outbound
        if removed:
            outbound.put(encode(CLOSE, conn_id))
        self._shutdown(sock)

    @staticmethod
    def _shutdown(sock):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass


def read_preamble(conn):
    """Return the job id, or None after closing a connection that is not a preamble."""
    conn.settimeout(5)
    buf = bytearray()
    try:
        while len(buf) < PREAMBLE:
            chunk = conn.recv(PREAMBLE - len(buf))
            if not chunk:
                break
            buf += chunk
    except OSError:
        buf = bytearray()
    job_id = bytes(buf[:32])
    valid = (
        len(buf) == PREAMBLE
        and buf[32:33] == b"\n"
        and all(c in b"0123456789abcdef" for c in job_id)
    )
    if not valid:
        conn.close()
        return None
    conn.settimeout(None)
    return job_id.decode()


class BrokerAcceptor:
    """Accept Firecracker guest-to-host connections on one VM's vsock port 1023."""

    def __init__(self, sock, route):
        self.sock = sock
        self.route = route
        self.stopping = threading.Event()
        self.sock.settimeout(0.5)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def alive(self):
        return self.thread.is_alive() and not self.stopping.is_set()

    def close(self):
        self.stopping.set()
        try:
            name = self.sock.getsockname()
        except OSError:
            name = None
        try:
            self.sock.close()
        except OSError:
            pass
        if isinstance(name, str):
            Path(name).unlink(missing_ok=True)
        self.thread.join(timeout=2)

    def _run(self):
        while not self.stopping.is_set():
            try:
                conn, _ = self.sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            job_id = read_preamble(conn)
            if job_id is not None:
                self.route(job_id, conn)
