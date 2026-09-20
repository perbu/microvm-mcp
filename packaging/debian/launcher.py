"""Entry point for the private packaged application; invoked with Python -I."""

import sys
from pathlib import Path

root = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(root / "app"), str(root / "dependencies")]

action = sys.argv.pop(1)
sys.argv[0] = "microvm-mcp-install-image" if action == "install-image" else "microvm-mcp"
if action == "install-image":
    from microvm_mcp.provision import main
else:
    from microvm_mcp.cli import main

try:
    main()
except (ValueError, OSError) as exc:
    print(f"microvm-mcp: {exc}", file=sys.stderr)
    sys.exit(1)
