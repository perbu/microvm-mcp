#!/usr/bin/env python3
"""Optional model-free fast-agent adapter probe. This program runs inside its Agent VM."""

import json
import sys


def emit(value):
    print(json.dumps(value), flush=True)


for line in sys.stdin:
    envelope = json.loads(line)
    if envelope.get("warmup"):
        emit({"output": {"ready": True}})
        continue
    request = envelope.get("input", {})
    emit(
        {
            "type": "tool_call",
            "id": "probe",
            "tool": request["tool"],
            "args": request.get("args", {}),
        }
    )
    result = json.loads(sys.stdin.readline())
    emit({"output": result, "state": {}})
