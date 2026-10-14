"""Bridge stdin/stdout to a guest vsock stream, e.g. as an MCP client's stdio command."""

import os
import sys
import threading
from urllib.parse import urlsplit

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from .streams import CHUNK


def stream_url(endpoint, vm_id, port):
    url = urlsplit(endpoint)
    if (
        url.scheme not in ("https", "http")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise ValueError("endpoint must be an HTTP(S) origin without credentials or path")
    scheme = "wss" if url.scheme == "https" else "ws"
    return f"{scheme}://{url.netloc}/v1/vms/{vm_id}/streams/{port}"


def attach(endpoint, token, vm_id, port, stdin=None, stdout=None, send_eof=True):
    """Copy until the stream closes. Returns a process exit status.

    Firecracker's vsock bridge has no half-close: after the EOF sent at the end of
    stdin, the guest reads the remaining data and EOF, but cannot reply. Downloads
    set send_eof=False and let the guest close the stream.
    """
    stdin = sys.stdin.buffer if stdin is None else stdin
    stdout = sys.stdout.buffer if stdout is None else stdout
    try:
        ws = connect(
            stream_url(endpoint, vm_id, port),
            additional_headers={"Authorization": f"Bearer {token}"},
            max_size=None,
            open_timeout=60,
            compression=None,
        )
    except InvalidStatus as exc:
        body = exc.response.body.decode(errors="replace")[:512]
        print(f"microvm-mcp attach: HTTP {exc.response.status_code} {body}", file=sys.stderr)
        return 1

    def upload():
        try:
            while data := os.read(stdin.fileno(), CHUNK):
                ws.send(data)
            if send_eof:
                ws.send(b"")  # EOF: the guest reads end-of-file.
        except (ConnectionClosed, OSError):
            pass

    with ws:
        threading.Thread(target=upload, daemon=True).start()
        try:
            for message in ws:
                if isinstance(message, str):
                    message = message.encode()
                stdout.write(message)
                stdout.flush()
        except ConnectionClosed:
            pass
        except BrokenPipeError:
            return 1
    stdout.close()
    if ws.close_code != 1000:
        detail = f"{ws.close_code} {ws.close_reason or ''}".strip()
        print(f"microvm-mcp attach: stream ended: {detail}", file=sys.stderr)
        return 1
    return 0
