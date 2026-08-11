from __future__ import annotations

import hashlib
from typing import Any

from .tool_authority import canonical_json

WORKER_SCHEMA_VERSION = "1"
WORKER_ENTRYPOINT_IMPLEMENTATION_IDENTITY = "sovereign_claw.worker_entrypoint.v2"

# Immutable, server-owned reviewed mapping for subprocess worker dispatch.
SUBPROCESS_WORKER_HANDLER_REGISTRY: dict[str, str] = {
    "builtin.echo_text.in_process": "sovereign_claw.tools_basic:echo_text",
    "builtin.list_directory.in_process": "sovereign_claw.tools_basic:scoped_list_directory",
    "builtin.read_text_file.in_process": "sovereign_claw.tools_basic:scoped_read_text_file",
    "builtin.write_json_file.in_process": "sovereign_claw.tools_basic:scoped_write_json_file",
}


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


SUBPROCESS_WORKER_HANDLER_REGISTRY_IDENTITY = _sha256_json(SUBPROCESS_WORKER_HANDLER_REGISTRY)
SUBPROCESS_WORKER_BUILD_IDENTITY = _sha256_json(
    {
        "worker_schema_version": WORKER_SCHEMA_VERSION,
        "entrypoint_identity": WORKER_ENTRYPOINT_IMPLEMENTATION_IDENTITY,
        "handler_registry_identity": SUBPROCESS_WORKER_HANDLER_REGISTRY_IDENTITY,
    }
)


def runtime_handler_registry_identity(handlers: dict[str, Any]) -> str:
    material: dict[str, str] = {}
    for handler_id, handler in sorted(handlers.items()):
        material[handler_id] = f"{handler.__module__}:{handler.__qualname__}"
    return _sha256_json(material)
