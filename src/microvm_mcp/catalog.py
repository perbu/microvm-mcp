"""One catalog for MCP, HTTP validation, and optional client adapters."""

ID = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
KEY = {"type": "string", "minLength": 1, "maxLength": 128}
PATH = {"type": "string", "minLength": 1, "maxLength": 1024}


def tool(name, description, properties=None, required=()):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
            "additionalProperties": False,
        },
    }


TOOLS = [
    tool(
        "vm_create",
        "Create a private Debian development VM; retry with the same request_id.",
        {
            "request_id": KEY,
            "network": {"enum": ["off", "on"], "default": "off"},
            "vcpus": {"type": "integer", "minimum": 1, "maximum": 8, "default": 2},
            "memory_mib": {"type": "integer", "minimum": 256, "maximum": 8192, "default": 1024},
            "idle_seconds": {"type": "integer", "minimum": 0, "maximum": 86400, "default": 300},
            "idle_mode": {"enum": ["pause", "hibernate"], "default": "hibernate"},
        },
        ["request_id"],
    ),
    tool("vm_list", "List VMs belonging to your workspace."),
    tool("vm_status", "Read VM status without waking it.", {"vm_id": ID}, ["vm_id"]),
    tool(
        "vm_exec",
        "Start a shell command in the VM. Returns a job handle; poll its output.",
        {
            "vm_id": ID,
            "request_id": KEY,
            "command": {"type": "string", "minLength": 1, "maxLength": 32768},
            "cwd": {**PATH, "default": "."},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600, "default": 300},
        },
        ["vm_id", "request_id", "command"],
    ),
    tool(
        "vm_job_status",
        "Read job status; a suspended VM stays suspended.",
        {
            "vm_id": ID,
            "job_id": ID,
        },
        ["vm_id", "job_id"],
    ),
    tool(
        "vm_job_output",
        "Read bounded combined stdout/stderr using a byte cursor; resumes the VM.",
        {
            "vm_id": ID,
            "job_id": ID,
            "cursor": {"type": "integer", "minimum": 0, "default": 0},
        },
        ["vm_id", "job_id"],
    ),
    tool(
        "vm_job_cancel",
        "Terminate a job and its descendants; resumes the VM.",
        {
            "vm_id": ID,
            "job_id": ID,
        },
        ["vm_id", "job_id"],
    ),
    tool(
        "vm_read_file",
        "Read a workspace file as base64 (up to 128 KiB); resumes the VM.",
        {
            "vm_id": ID,
            "path": PATH,
        },
        ["vm_id", "path"],
    ),
    tool(
        "vm_write_file",
        "Write a base64 workspace file (up to 128 KiB); resumes the VM.",
        {
            "vm_id": ID,
            "path": PATH,
            "data_base64": {"type": "string", "maxLength": 174764},
        },
        ["vm_id", "path", "data_base64"],
    ),
    tool(
        "vm_suspend",
        "Pause or hibernate a VM. Active jobs require explicit force.",
        {
            "vm_id": ID,
            "mode": {"enum": ["pause", "hibernate"], "default": "hibernate"},
            "force": {"type": "boolean", "default": False},
        },
        ["vm_id"],
    ),
    tool("vm_resume", "Resume a suspended VM with its state intact.", {"vm_id": ID}, ["vm_id"]),
    tool(
        "vm_destroy",
        "Permanently delete a VM, its jobs, files, and snapshots.",
        {
            "vm_id": ID,
        },
        ["vm_id"],
    ),
]
BY_NAME = {t["name"]: t for t in TOOLS}
