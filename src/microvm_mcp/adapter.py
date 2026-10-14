"""Generate optional fast-agent host_fetch configuration, without importing it."""

import json
import re
from urllib.parse import urlsplit

from .catalog import TOOLS


def inline(value):
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{json.dumps(k)} = {inline(v)}" for k, v in value.items()) + " }"
    if isinstance(value, list):
        return "[" + ", ".join(inline(v) for v in value) + "]"
    return json.dumps(value)


def manifest(endpoint, secret_ref="microvm_workspace", vm_class="exec", tools=TOOLS):
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
    for value in (secret_ref, vm_class):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError("invalid manifest identifier")
    port = url.port or (443 if url.scheme == "https" else 80)
    out = [
        "# Append to an existing fast-agent tools manifest with this VM class.",
        "# One credential = one workspace. All sessions in this deployment share it.",
    ]
    for tool in tools:
        name = tool["name"]
        out.extend(
            [
                f"\n[tools.microvm_{name}]",
                f"name = {json.dumps(name)}",
                f"vm_class = {json.dumps(vm_class)}",
                'transport = "host_fetch"',
                "timeout_ms = 120000",
                "max_output_bytes = 524288",
                "max_transport_bytes = 524288",
                "allow_private_networks = true",
                f"secret_refs = [{json.dumps(secret_ref)}]",
                f"description = {json.dumps(tool['description'])}",
                f"input_schema = {inline(tool['inputSchema'])}",
                f"[tools.microvm_{name}.credential]",
                'header = "Authorization"',
                'prefix = "Bearer "',
                f"secret_ref = {json.dumps(secret_ref)}",
                f"[tools.microvm_{name}.egress]",
                f"scheme = {json.dumps(url.scheme)}",
                f"host = {json.dumps(url.hostname)}",
                f"ports = [{port}]",
                'methods = ["POST"]',
                f'path_prefixes = ["/v1/tools/{name}"]',
                "public_only = true",
            ]
        )
    return "\n".join(out) + "\n"
