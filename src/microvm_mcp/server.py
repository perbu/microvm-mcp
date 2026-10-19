import asyncio
import contextvars
import json
import logging
import re
import socket
import threading
from contextlib import asynccontextmanager

import anyio
import mcp.types as types
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

from .errors import ServiceError
from .streams import CHUNK, Rate

TOKEN = contextvars.ContextVar("workspace_credential")
LOG = logging.getLogger(__name__)
MAX_BODY = 256 * 1024
# Client frames above this close the stream; the attach client sends CHUNK-sized frames.
MAX_FRAME = 16 * CHUNK
VM_ID = re.compile(r"[a-f0-9]{32}")
RECHECK_SECONDS = 5


async def deny(websocket, response):
    # Prefer a real HTTP status and JSON error; otherwise a policy-violation close.
    if "websocket.http.response" in websocket.scope.get("extensions", {}):
        await websocket.send_denial_response(response)
    else:
        await websocket.close(1008)


class AccessBoundary:
    """Authenticate every HTTP and WebSocket request before any routing."""

    def __init__(self, app, service, allowed_hosts):
        self.app, self.service = app, service
        self.allowed_hosts = set(allowed_hosts)

    def check(self, headers):
        """Return (token, None) or (None, denial response)."""
        if headers.get("host") not in self.allowed_hosts:
            return None, JSONResponse({"error": "unrecognized host"}, 403)
        # This is an agent API, not a browser application. No ambient cookie authority.
        if "origin" in headers:
            return None, JSONResponse({"error": "browser requests disabled"}, 403)
        auth = headers.getlist("authorization")
        if len(auth) != 1 or not auth[0].startswith("Bearer "):
            return None, JSONResponse(
                {"error": "credential required"}, 401, headers={"WWW-Authenticate": "Bearer"}
            )
        token = auth[0][7:]
        try:
            self.service.store.authenticate(token)
        except ServiceError:
            return None, JSONResponse(
                {"error": "invalid credential"}, 401, headers={"WWW-Authenticate": "Bearer"}
            )
        return token, None

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        token, denial = self.check(Headers(scope=scope))
        if scope["type"] == "websocket":
            if denial is not None:
                return await deny(WebSocket(scope, receive, send), denial)
            reset = TOKEN.set(token)
            try:
                return await self.app(scope, receive, send)
            finally:
                TOKEN.reset(reset)
        if denial is not None:
            return await denial(scope, receive, send)
        request = Request(scope, receive)
        # Bound bodies for both transports, including chunked HTTP. Do not accumulate
        # arbitrary bodies inside Starlette/JSON decoding or before authentication.
        body = bytearray()
        try:
            with anyio.fail_after(10):
                async for chunk in request.stream():
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        return await JSONResponse({"error": "body too large"}, 413)(
                            scope, receive, send
                        )
        except TimeoutError:
            return await JSONResponse({"error": "body timeout"}, 408)(scope, receive, send)
        consumed = False

        async def bounded_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        reset = TOKEN.set(token)
        try:
            return await self.app(scope, bounded_receive, send)
        finally:
            TOKEN.reset(reset)


def create_app(service, allowed_hosts):
    async def dispatch(name, args):
        try:
            return await anyio.to_thread.run_sync(service.call, TOKEN.get(), name, args)
        except ServiceError:
            raise
        except Exception:
            LOG.exception("operation failed: %s", name)
            raise ServiceError(
                "backend_failure", "Operation failed; inspect VM status", 502
            ) from None

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[types.Tool(**tool) for tool in service.tools])

    async def call_tool(ctx, params):
        try:
            result = await dispatch(params.name, params.arguments or {})
            error = False
        except ServiceError as exc:
            result, error = exc.body(), True
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(result))],
            structuredContent=result,
            isError=error,
        )

    async def http_tool(request):
        try:
            args = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "invalid JSON"}, 400)
        try:
            return JSONResponse(await dispatch(request.path_params["name"], args))
        except ServiceError as exc:
            return JSONResponse(exc.body(), exc.status)

    async def pump(websocket, stream, token):
        """Copy bytes both ways until the guest closes its end (close code 1000).

        A client's empty binary frame is EOF for the guest. Firecracker's vsock bridge
        has no half-close: the guest reads the remaining data and EOF, then the
        connection is closed in both directions.
        """
        sock = stream.sock
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        outcome = []
        client_eof = False

        async with anyio.create_task_group() as group:

            def finish(code, reason=""):
                if not outcome:
                    outcome.append((code, reason))
                group.cancel_scope.cancel()

            async def from_guest():
                rate = Rate(service.stream_rate_bytes)
                try:
                    while data := await loop.sock_recv(sock, CHUNK):
                        await rate.take(len(data))
                        stream.from_guest += len(data)
                        await websocket.send_bytes(data)
                except OSError:
                    pass
                except Exception:
                    return finish(None)  # Client is gone; nothing to close.
                if stream.reason is not None:
                    return finish(1001, stream.reason)
                finish(1000)

            async def from_client():
                nonlocal client_eof
                rate = Rate(service.stream_rate_bytes)
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        return finish(None)
                    data = message.get("bytes")
                    if data is None:
                        data = (message.get("text") or "").encode()
                    if client_eof:
                        return finish(1002, "data after EOF")
                    try:
                        if not data:
                            sock.shutdown(socket.SHUT_WR)
                            client_eof = True
                            continue
                        await rate.take(len(data))
                        await loop.sock_sendall(sock, data)
                    except OSError:
                        return finish(1001, stream.reason or "guest connection closed")
                    stream.to_guest += len(data)

            async def watchdog():
                # Revocation or expiry ends open streams too, not only new requests.
                while True:
                    await anyio.sleep(RECHECK_SECONDS)
                    if stream.reason is not None:
                        return finish(1001, stream.reason)
                    try:
                        await anyio.to_thread.run_sync(service.store.authenticate, token)
                    except ServiceError:
                        stream.close("credential invalid")
                        return finish(1008, "credential invalid")

            group.start_soon(from_guest)
            group.start_soon(from_client)
            group.start_soon(watchdog)
        return outcome[0] if outcome else (1011, "stream error")

    async def pump_broker(websocket, session, token):
        """Run one attachment until it ends; recheck the credential meanwhile."""
        outcome = []

        async with anyio.create_task_group() as group:

            def finish(code, reason=""):
                if not outcome:
                    outcome.append((code, reason))
                group.cancel_scope.cancel()

            async def run():
                finish(*await session.run(websocket))

            async def watchdog():
                while True:
                    await anyio.sleep(RECHECK_SECONDS)
                    try:
                        await anyio.to_thread.run_sync(service.store.authenticate, token)
                    except ServiceError:
                        finish(1008, "credential invalid")
                        return session.pipe.detach("credential invalid", session)

            group.start_soon(run)
            group.start_soon(watchdog)
        return outcome[0] if outcome else (1011, "broker pipe error")

    async def broker_endpoint(websocket):
        vm_id = websocket.path_params["vm_id"]
        job_id = websocket.path_params["job_id"]
        token = TOKEN.get()
        offered = websocket.headers.getlist("x-broker-pipe")
        pipe_token = offered[0] if len(offered) == 1 else ""
        loop = asyncio.get_running_loop()
        try:
            if not VM_ID.fullmatch(vm_id) or not VM_ID.fullmatch(job_id):
                raise ServiceError("not_found", "Job not found", 404)
            # Shielded: an attached session must reach close_broker below.
            with anyio.CancelScope(shield=True):
                session = await anyio.to_thread.run_sync(
                    service.open_broker, token, vm_id, job_id, pipe_token, loop
                )
        except ServiceError as exc:
            return await deny(websocket, JSONResponse(exc.body(), exc.status))
        except Exception:
            LOG.exception("broker open failed")
            error = ServiceError("backend_failure", "Broker pipe failed; inspect VM status", 502)
            return await deny(websocket, JSONResponse(error.body(), error.status))
        code, reason = 1011, "broker pipe error"
        try:
            await websocket.accept()
            code, reason = await pump_broker(websocket, session, token)
        finally:
            # Non-blocking: it detaches only this session and logs metadata.
            service.close_broker(session, reason or "closed")
        if code is not None:
            try:
                await websocket.close(code, reason)
            except Exception:
                pass

    async def stream_endpoint(websocket):
        vm_id, port = websocket.path_params["vm_id"], websocket.path_params["port"]
        token = TOKEN.get()
        try:
            if not VM_ID.fullmatch(vm_id):
                raise ServiceError("not_found", "VM not found", 404)
            # Shielded: a registered stream must reach close_stream below.
            with anyio.CancelScope(shield=True):
                stream = await anyio.to_thread.run_sync(service.open_stream, token, vm_id, port)
        except ServiceError as exc:
            return await deny(websocket, JSONResponse(exc.body(), exc.status))
        except Exception:
            LOG.exception("stream open failed")
            error = ServiceError("backend_failure", "Stream failed; inspect VM status", 502)
            return await deny(websocket, JSONResponse(error.body(), error.status))
        code, reason = 1011, "stream error"
        try:
            await websocket.accept()
            code, reason = await pump(websocket, stream, token)
        finally:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(service.close_stream, stream, reason or "closed")
        if code is not None:
            try:
                await websocket.close(code, reason)
            except Exception:
                pass

    @asynccontextmanager
    async def lifespan(server):
        thread = threading.Thread(target=service.watch, daemon=True)
        thread.start()
        try:
            yield {}
        finally:
            service.stopping.set()
            await anyio.to_thread.run_sync(thread.join, 15)
            await anyio.to_thread.run_sync(service.close)

    server = Server(
        "microvm-mcp",
        version="0.1.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        lifespan=lifespan,
    )
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=MAX_BODY,
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts, allowed_origins=[]
        ),
        custom_starlette_routes=[
            Route("/v1/tools/{name}", http_tool, methods=["POST"]),
            WebSocketRoute("/v1/vms/{vm_id}/streams/{port:int}", stream_endpoint),
            WebSocketRoute("/v1/vms/{vm_id}/jobs/{job_id}/broker", broker_endpoint),
        ],
    )
    return AccessBoundary(app, service, allowed_hosts)
