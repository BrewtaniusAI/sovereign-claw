from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from typing import Any

from .tool_authority import canonical_json

WORKER_SCHEMA_VERSION = "1"
WORKER_ENTRYPOINT_MODULE = "sovereign_claw.worker_entrypoint"
WORKER_MANIFEST_MODULE = "sovereign_claw.worker_manifest"
WORKER_RUNTIME_MODULES: tuple[str, ...] = (
    "sovereign_claw.execution_boundary",
    "sovereign_claw.tool_authority",
    WORKER_ENTRYPOINT_MODULE,
    WORKER_MANIFEST_MODULE,
)

# Immutable, server-owned reviewed mapping for subprocess worker dispatch.
SUBPROCESS_WORKER_HANDLER_REGISTRY: dict[str, str] = {
    "builtin.echo_text.in_process": "sovereign_claw.tools_basic:echo_text",
    "builtin.list_directory.in_process": "sovereign_claw.tools_basic:scoped_list_directory",
    "builtin.read_text_file.in_process": "sovereign_claw.tools_basic:scoped_read_text_file",
    "builtin.write_json_file.in_process": "sovereign_claw.tools_basic:scoped_write_json_file",
}


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_module_artifact_bytes(module_name: str) -> bytes:
    spec = importlib.util.find_spec(module_name)
    if spec is None or not spec.origin:
        raise RuntimeError(f"Unable to resolve artifact origin for module {module_name!r}")
    origin = Path(spec.origin)
    if origin.suffix == ".pyc":
        py_origin = origin.with_suffix(".py")
        if py_origin.exists():
            origin = py_origin
    try:
        return origin.read_bytes()
    except Exception as exc:
        raise RuntimeError(f"Unable to read artifact bytes for module {module_name!r}") from exc


def _split_handler_target(target: str) -> tuple[str, str]:
    module_name, _, qualname = target.partition(":")
    if not module_name or not qualname:
        raise RuntimeError(f"Invalid worker handler target {target!r}")
    return module_name, qualname


def _handler_registry_material(registry: dict[str, str]) -> dict[str, dict[str, str]]:
    material: dict[str, dict[str, str]] = {}
    for handler_id, target in sorted(registry.items()):
        module_name, qualname = _split_handler_target(target)
        material[handler_id] = {
            "target": f"{module_name}:{qualname}",
            "module_sha256": _sha256_bytes(_read_module_artifact_bytes(module_name)),
        }
    return material


def _worker_runtime_material() -> dict[str, str]:
    material: dict[str, str] = {}
    for module_name in sorted(set(WORKER_RUNTIME_MODULES)):
        material[module_name] = _sha256_bytes(_read_module_artifact_bytes(module_name))
    return material


def compute_subprocess_worker_handler_registry_identity(
    registry: dict[str, str] | None = None,
) -> str:
    return _sha256_json(_handler_registry_material(registry or SUBPROCESS_WORKER_HANDLER_REGISTRY))


def compute_subprocess_worker_build_identity(handler_registry_identity: str) -> str:
    return _sha256_json(
        {
            "worker_schema_version": WORKER_SCHEMA_VERSION,
            "worker_runtime_modules": _worker_runtime_material(),
            "handler_registry_identity": handler_registry_identity,
        }
    )


SUBPROCESS_WORKER_HANDLER_REGISTRY_IDENTITY = compute_subprocess_worker_handler_registry_identity()
SUBPROCESS_WORKER_BUILD_IDENTITY = compute_subprocess_worker_build_identity(
    SUBPROCESS_WORKER_HANDLER_REGISTRY_IDENTITY
)


def runtime_handler_registry_identity(handlers: dict[str, Any]) -> str:
    material: dict[str, dict[str, str]] = {}
    for handler_id, handler in sorted(handlers.items()):
        module_name = getattr(handler, "__module__", "")
        qualname = getattr(handler, "__qualname__", "")
        if not module_name or not qualname:
            raise RuntimeError(f"Invalid runtime worker handler identity for {handler_id!r}")
        material[handler_id] = {
            "target": f"{module_name}:{qualname}",
            "module_sha256": _sha256_bytes(_read_module_artifact_bytes(module_name)),
        }
    return _sha256_json(material)
