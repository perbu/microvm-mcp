import contextvars
import json
import logging
import threading
from contextlib import asynccontextmanager

import anyio
import mcp.types as types
from mcp.server import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .catalog import TOOLS
from .errors import ServiceError

TOKEN = contextvars.ContextVar("workspace_credential")
LOG = logging.getLogger(__name__)
MAX_BODY = 256 * 1024


class AccessBoundary:
    """Authenticate every HTTP request, before MCP dispatch or HTTP tool routing."""

    def __init__(self, app, service, allowed_hosts):
        self.app, self.service = app, service
        self.allowed_hosts = set(allowed_hosts)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        if request.headers.get("host") not in self.allowed_hosts:
            return await JSONResponse({"error": "unrecognized host"}, 403)(scope, receive, send)
        # This is an agent API, not a browser application. No ambient cookie authority.
        if "origin" in request.headers:
            return await JSONResponse({"error": "browser requests disabled"}, 403)(
                scope, receive, send
            )
        auth = request.headers.getlist("authorization")
        if len(auth) != 1 or not auth[0].startswith("Bearer "):
            return await JSONResponse(
                {"error": "credential required"}, 401, headers={"WWW-Authenticate": "Bearer"}
            )(scope, receive, send)
        token = auth[0][7:]
        try:
            self.service.store.authenticate(token)
        except ServiceError:
            return await JSONResponse(
                {"error": "invalid credential"}, 401, headers={"WWW-Authenticate": "Bearer"}
            )(scope, receive, send)
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
        return types.ListToolsResult(tools=[types.Tool(**tool) for tool in TOOLS])

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
        custom_starlette_routes=[Route("/v1/tools/{name}", http_tool, methods=["POST"])],
    )
    return AccessBoundary(app, service, allowed_hosts)
