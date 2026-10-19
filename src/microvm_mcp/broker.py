"""Job-scoped guest-to-host broker pipe. Copy bytes; never interpret or log them."""

import asyncio
import collections
import selectors
import socket
import struct
import threading
import time
from pathlib import Path

import anyio

from .errors import ServiceError
from .streams import Rate

OPEN, DATA, CLOSE = 1, 2, 3
MAX_PAYLOAD = 65536
MAX_PENDING = 32  # Guest connections waiting for a controller, per pipe.
MAX_CONNECTIONS = 64  # Guest connections per attachment.
MAX_ATTACHED = 4  # Attached pipes per VM; cli.py reserves this many connections per VM.
# Controller bytes queued for one guest connection. A guest that leaves them
# unread for STALL_SECONDS loses that connection, not the whole pipe.
MAX_BUFFERED = 1024 * 1024
STALL_SECONDS = 10
# Guest-to-host preamble: the 32-hex job id, the 64-hex route secret the host
# gave that job's listener, and a newline. Raw bytes follow.
JOB_HEX, ROUTE_HEX = 32, 64
PREAMBLE = JOB_HEX + ROUTE_HEX + 1
HANDSHAKE_SECONDS = 2
MAX_HANDSHAKES = 64
HEX = frozenset(b"0123456789abcdef")


class ProtocolError(Exception):
    pass


def encode(kind, conn_id, payload=b""):
    if kind == DATA:
        return struct.pack(">BII", DATA, conn_id, len(payload)) + payload
    return struct.pack(">BI", kind, conn_id)


def shutdown(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def discard(sock):
    shutdown(sock)
    try:
        sock.close()
    except OSError:
        pass


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
    """One job's pipe. It outlives attachments until the job or VM is gone."""

    def __init__(self, vm_id, owner, job_id, token_hash, route):
        self.vm_id, self.owner, self.job_id = vm_id, owner, job_id
        self.token_hash = token_hash
        self.route = route
        self.lock = threading.Lock()
        self.session = None
        self.pending = []
        # Set once the pipe can never attach again: destroyed, job finished.
        self.retired = None

    def add_guest(self, sock):
        """Called on the acceptor thread; never blocks."""
        with self.lock:
            session = self.session
            if self.retired is None and session is None and len(self.pending) < MAX_PENDING:
                self.pending.append(sock)
                return
        if session is None or not session.offer(sock):
            discard(sock)

    def attach(self, loop, rate):
        with self.lock:
            if self.retired is not None:
                raise ServiceError("not_found", "Job not found", 404)
            if self.session is not None:
                raise ServiceError("conflict", "Broker pipe is already attached", 409)
            session = self.session = Session(self, loop, rate)
            pending, self.pending = self.pending, []
        for sock in pending:
            if not session.offer(sock):
                discard(sock)
        return session

    def detach(self, reason, session=None):
        """End an attachment. A stale session's late cleanup leaves a newer one alone.

        The service detaches without a session on suspension; that also drops guest
        connections still waiting for a controller.
        """
        with self.lock:
            current = self.session
            if session is not None and session is not current:
                return
            self.session = None
            pending = []
            if session is None:
                pending, self.pending = self.pending, []
        for sock in pending:
            discard(sock)
        if current is not None:
            current.stop(reason)

    def retire(self, reason):
        with self.lock:
            if self.retired is None:
                self.retired = reason
        self.detach(self.retired)


class Conn:
    """One guest connection inside an attachment. Loop thread only."""

    def __init__(self, conn_id, sock):
        self.id, self.sock = conn_id, sock
        self.queue = collections.deque()
        self.buffered = 0
        self.wake = asyncio.Event()
        self.drained = asyncio.Event()
        self.drained.set()
        self.finishing = False  # Controller sent CLOSE; flush, then close.
        self.closed = False
        self.scope = None


class Session:
    """One controller attachment, run on the event loop with non-blocking sockets.

    offer() and stop() may be called from any thread. Everything else runs on
    the loop, so a guest that stops reading cannot hold a worker thread.
    """

    def __init__(self, pipe, loop, rate):
        self.pipe, self.loop = pipe, loop
        # Same per-direction limit as plain streams.
        self.rate_in, self.rate_out = Rate(rate), Rate(rate)
        self.opened_at = time.monotonic()
        self.to_guest = self.from_guest = 0
        self.reason = None
        self.logged = False
        self.lock = threading.Lock()
        # Every guest socket this session owns, so stop() can shut them down at once.
        self.socks = set()
        self.closed = False
        # Loop-thread state.
        self.arrivals = collections.deque()
        self.arrived = asyncio.Event()
        self.stopped = asyncio.Event()
        self.conns = {}
        self.next_id = 1
        self.wrapped = False
        self.running = False
        self.send_lock = asyncio.Lock()

    def offer(self, sock):
        with self.lock:
            if self.closed:
                return False
            self.socks.add(sock)
        try:
            self.loop.call_soon_threadsafe(self._arrive, sock)
        except RuntimeError:  # The loop is gone.
            self._forget(sock)
            return False
        return True

    def stop(self, reason):
        with self.lock:
            if self.reason is None:
                self.reason = reason
            self.closed = True
            socks = list(self.socks)
        # Shut down now, not on the loop: suspension must not race guest bytes.
        for sock in socks:
            shutdown(sock)
        try:
            self.loop.call_soon_threadsafe(self._stop)
        except RuntimeError:
            pass

    def _forget(self, sock):
        with self.lock:
            self.socks.discard(sock)
        discard(sock)

    def _arrive(self, sock):
        if self.closed or len(self.conns) + len(self.arrivals) >= MAX_CONNECTIONS:
            self._forget(sock)
            return
        self.arrivals.append(sock)
        self.arrived.set()

    def _stop(self):
        self.stopped.set()
        if not self.running:
            self._cleanup()

    def _cleanup(self):
        with self.lock:
            self.closed = True
        while self.arrivals:
            self._forget(self.arrivals.popleft())
        # No conn task is running here: the task group has exited or never started.
        for conn in list(self.conns.values()):
            conn.closed = True
            self._forget(conn.sock)
        self.conns.clear()

    def _take_id(self):
        conn_id = self.next_id
        if conn_id == 2**32 - 1:
            self.next_id, self.wrapped = 1, True
        else:
            self.next_id = conn_id + 1
        return conn_id

    async def run(self, websocket):
        """Return a (code, reason) close, or (None, "") when the client is gone."""
        outcome = []
        self.running = True
        try:
            async with anyio.create_task_group() as group:

                def finish(code, reason=""):
                    if not outcome:
                        outcome.append((code, reason))
                    group.cancel_scope.cancel()

                async def stopped():
                    await self.stopped.wait()
                    finish(1001, self.reason or "closed")

                self.finish = finish
                self.websocket = websocket
                group.start_soon(stopped)
                group.start_soon(self._accept, group)
                group.start_soon(self._from_controller)
        finally:
            self.running = False
            self._cleanup()
        return outcome[0] if outcome else (1011, "broker pipe error")

    async def _send(self, frame):
        try:
            async with self.send_lock:
                await self.websocket.send_bytes(frame)
        except anyio.get_cancelled_exc_class():
            raise
        except Exception:
            self.finish(None)  # Client is gone; nothing to close.

    async def _accept(self, group):
        while True:
            await self.arrived.wait()
            self.arrived.clear()
            while self.arrivals:
                sock = self.arrivals.popleft()
                sock.setblocking(False)
                conn = Conn(self._take_id(), sock)
                self.conns[conn.id] = conn
                await self._send(encode(OPEN, conn.id))
                group.start_soon(self._serve, conn)

    async def _serve(self, conn):
        try:
            with anyio.CancelScope() as conn.scope:
                if conn.closed:
                    return
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(self._read_guest, conn)
                    tasks.start_soon(self._write_guest, conn)
        finally:
            await self._close(conn, notify=False)
            self._forget(conn.sock)

    async def _close(self, conn, *, notify):
        if conn.closed:
            return
        conn.closed = True
        self.conns.pop(conn.id, None)
        shutdown(conn.sock)
        conn.wake.set()
        conn.drained.set()
        if notify:
            # Before cancelling: the caller may be one of this connection's tasks.
            await self._send(encode(CLOSE, conn.id))
        if conn.scope is not None:
            conn.scope.cancel()

    async def _read_guest(self, conn):
        try:
            while data := await self.loop.sock_recv(conn.sock, MAX_PAYLOAD):
                await self.rate_out.take(len(data))
                self.from_guest += len(data)
                await self._send(encode(DATA, conn.id, data))
        except OSError:
            pass
        # Firecracker does not forward a guest half-close, so EOF here means the
        # guest connection is gone. Anything still queued for it is dropped.
        await self._close(conn, notify=True)

    async def _write_guest(self, conn):
        try:
            while True:
                while not conn.queue:
                    if conn.finishing:
                        return await self._close(conn, notify=False)
                    conn.wake.clear()
                    await conn.wake.wait()
                data = conn.queue.popleft()
                await self.loop.sock_sendall(conn.sock, data)
                conn.buffered -= len(data)
                self.to_guest += len(data)
                conn.drained.set()
        except OSError:
            await self._close(conn, notify=True)

    async def _from_controller(self):
        parser = Parser()
        while True:
            message = await self.websocket.receive()
            if message["type"] == "websocket.disconnect":
                return self.finish(None)
            data = message.get("bytes")
            if data is None:
                return self.finish(1002, "binary frames required")
            try:
                frames = parser.feed(data)
            except ProtocolError:
                return self.finish(1002, "bad frame")
            for kind, conn_id, payload in frames:
                if kind == OPEN:
                    return self.finish(1002, "bad frame")
                conn = self.conns.get(conn_id)
                if conn is None:
                    # A guest connection may close while the controller still writes.
                    if self.wrapped or conn_id < self.next_id:
                        continue
                    return self.finish(1002, "unknown connection")
                if kind == CLOSE:
                    conn.finishing = True
                    conn.wake.set()
                    continue
                await self.rate_in.take(len(payload))
                await self._queue(conn, payload)

    async def _queue(self, conn, payload):
        # Backpressure: stop reading the websocket while this guest drains, but
        # only for so long; a stalled guest must not hold the other connections.
        with anyio.move_on_after(STALL_SECONDS):
            while not conn.closed and conn.buffered + len(payload) > MAX_BUFFERED:
                conn.drained.clear()
                await conn.drained.wait()
        if conn.closed or conn.finishing:
            return
        if conn.buffered + len(payload) > MAX_BUFFERED:
            return await self._close(conn, notify=True)
        conn.queue.append(payload)
        conn.buffered += len(payload)
        conn.wake.set()


def parse_preamble(buf):
    """Return (job_id, route) from a complete preamble, or None."""
    if len(buf) != PREAMBLE or buf[-1:] != b"\n" or not HEX.issuperset(buf[:-1]):
        return None
    return bytes(buf[:JOB_HEX]).decode(), bytes(buf[JOB_HEX:-1])


class BrokerAcceptor:
    """Accept Firecracker guest-to-host connections on one VM's vsock port 1023.

    One selector thread reads every preamble without blocking, so a guest that
    connects and stays silent costs a slot for HANDSHAKE_SECONDS, not the thread.
    """

    def __init__(self, sock, route):
        self.sock = sock
        self.route = route
        name = sock.getsockname()
        self.path = Path(name) if isinstance(name, str) and name else None
        self.stopping = threading.Event()
        self.wake_read, self.wake_write = socket.socketpair()
        self.sock.setblocking(False)
        self.handshakes = {}
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def alive(self):
        return self.thread.is_alive() and not self.stopping.is_set()

    def close(self):
        self.stopping.set()
        try:
            self.wake_write.send(b"\0")
        except OSError:
            pass
        self.thread.join(timeout=2)
        if self.path is not None:
            self.path.unlink(missing_ok=True)

    def _run(self):
        selector = selectors.DefaultSelector()
        selector.register(self.sock, selectors.EVENT_READ)
        selector.register(self.wake_read, selectors.EVENT_READ)
        try:
            while not self.stopping.is_set():
                for key, _ in selector.select(0.25):
                    if key.fileobj is self.sock:
                        if not self._accept(selector):
                            return
                    elif key.fileobj is not self.wake_read:
                        self._read(selector, key.fileobj)
                now = time.monotonic()
                for conn, (deadline, _) in list(self.handshakes.items()):
                    if now >= deadline:
                        self._drop(selector, conn)
        finally:
            for conn in list(self.handshakes):
                self._drop(selector, conn)
            selector.close()
            for sock in (self.sock, self.wake_read, self.wake_write):
                sock.close()

    def _accept(self, selector):
        while True:
            try:
                conn, _ = self.sock.accept()
            except BlockingIOError:
                return True
            except OSError:
                return False
            if len(self.handshakes) >= MAX_HANDSHAKES:
                discard(conn)
                continue
            conn.setblocking(False)
            self.handshakes[conn] = (time.monotonic() + HANDSHAKE_SECONDS, bytearray())
            selector.register(conn, selectors.EVENT_READ)

    def _read(self, selector, conn):
        _, buf = self.handshakes[conn]
        try:
            # Never read past the preamble; the rest belongs to the controller.
            chunk = conn.recv(PREAMBLE - len(buf))
        except BlockingIOError:
            return
        except OSError:
            chunk = b""
        if not chunk:
            return self._drop(selector, conn)
        buf += chunk
        if len(buf) < PREAMBLE:
            return
        selector.unregister(conn)
        del self.handshakes[conn]
        parsed = parse_preamble(buf)
        if parsed is None:
            return discard(conn)
        try:
            self.route(*parsed, conn)
        except Exception:
            discard(conn)

    def _drop(self, selector, conn):
        self.handshakes.pop(conn, None)
        try:
            selector.unregister(conn)
        except (KeyError, ValueError):
            pass
        discard(conn)
