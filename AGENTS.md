# Standalone project

This directory must remain extractable as an independent project. Do not add
imports or build dependencies on the parent repository. Keep optional integrations
as configuration generators or separate clients of the public HTTP/MCP API.

Read README.md before changing lifecycle, authorization, networking or storage.
Run the Python lint/format checks and pytest. Changes to the actual VM backend
also require scripts/smoke.py on a Linux KVM host; missing prerequisites fail.
Never introduce a host-subprocess backend that executes model-supplied commands.
Guest supervisor code executes only inside the full Linux microVM.

The owner comes only from authenticated workspace credentials. VM IDs, MCP
sessions, and tool arguments never carry ownership authority. Keep credentials
out of both the persistent Agent VM and the development guest.
