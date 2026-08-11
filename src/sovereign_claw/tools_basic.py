"""
tools_basic.py — Built-in Tools
================================
A small set of deterministic tools for Orchestrator registration.
These are safe to use in any manifold (no network, no shell).

DRIFT-10 FIX (v2.0.0)
----------------------
The original module exposed plain functions with no metadata, schema, or
safety classification.  The Orchestrator registered them by name only,
meaning malformed kwargs from an LLM caused uncontrolled exceptions rather
than governed drift penalties.

This version adds:
  - ToolSpec dataclass: name, description, required_kwargs, safety_tier
  - TOOL_REGISTRY: authoritative mapping of name → (function, ToolSpec)
  - validate_kwargs(): raises TypeError with a governed message when required
    kwargs are missing, so KitaevZeroMode can translate the error to a
    bounded drift penalty instead of propagating a raw exception.
  - register_all(): convenience helper to register all built-in tools with
    an Orchestrator instance.

AUTHORITY UPDATE (v3.0.0 / #40)
--------------------------------
Adds:
  - ToolSpecV1 authority records for each built-in tool.
  - FilesystemCapability: server-created scoped root with path security
    (no absolute paths, no `..`, no NUL, no symlink escapes, no
    device/special files, byte caps, atomic writes).
  - Scoped filesystem functions (governed production lane).
  - register_all() also registers ToolRegistryEntry records when
    orchestrator.tool_registry is available.
  - Legacy unrestricted path wrappers remain for development compatibility
    and are NOT approvable in governed mode.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .tool_authority import (
    PostconditionFailedError,
    PostconditionValidatorRegistry,
    ToolRegistry,
    ToolSpecV1,
    canonical_json,
    make_registry_entry,
)

# ── ToolSpec (legacy compatibility) ───────────────────────────────────────────
SafetyTier = Literal["READ_ONLY", "WRITE_LOCAL", "NETWORK", "SHELL"]


@dataclass
class ToolSpec:
    """
    Metadata descriptor for a registered tool.

    Fields
    ------
    name            : Canonical tool name (matches Orchestrator key)
    description     : One-sentence description for LLM context injection
    required_kwargs : List of kwarg names that MUST be present
    safety_tier     : Highest-risk operation the tool performs
    """

    name: str
    description: str
    required_kwargs: list[str] = field(default_factory=list)
    safety_tier: SafetyTier = "READ_ONLY"


def validate_kwargs(spec: ToolSpec, kwargs: dict[str, Any]) -> None:
    """
    Raise TypeError if any required kwarg is missing.
    The error message is structured so KitaevZeroMode can surface it to the
    LLM as a governed constraint message rather than a raw traceback.
    """
    missing = [k for k in spec.required_kwargs if k not in kwargs]
    if missing:
        raise TypeError(
            f"Tool '{spec.name}' missing required kwargs: {missing}. Recalculate approach vector."
        )


# ── FilesystemCapability (governed production lane) ────────────────────────────

#: Maximum size of a single file read/write in bytes (governed).
_MAX_FILE_READ_BYTES: int = 4 * 1024 * 1024  # 4 MiB
_MAX_FILE_WRITE_BYTES: int = 4 * 1024 * 1024  # 4 MiB
_MAX_LIST_ENTRIES: int = 4096

#: Module-level capability registry: root_id → FilesystemCapability
_CAPABILITY_REGISTRY: dict[str, FilesystemCapability] = {}


@dataclass
class FilesystemCapability:
    """
    Server-created scoped filesystem root.

    Only paths relative to *root* (no ``..``, no absolute, no NUL,
    no symlink escapes, no device/special files) are permitted.
    """

    root: Path
    root_id: str
    max_read_bytes: int = _MAX_FILE_READ_BYTES
    max_write_bytes: int = _MAX_FILE_WRITE_BYTES
    max_list_entries: int = _MAX_LIST_ENTRIES
    allow_overwrite: bool = False

    def _safe_resolve(self, relative_path: str) -> Path:
        """
        Validate *relative_path* and return the resolved absolute Path.
        Raises ValueError for any path traversal or unsafe attempt.
        """
        if not isinstance(relative_path, str):
            raise ValueError("relative_path must be a string")
        if "\x00" in relative_path:
            raise ValueError("relative_path must not contain NUL bytes")
        p = Path(relative_path)
        if p.is_absolute():
            raise ValueError("relative_path must not be absolute")
        # Reject any '..' component
        for part in p.parts:
            if part == "..":
                raise ValueError("relative_path must not contain '..' components")
        resolved = (self.root / p).resolve()
        # Verify the resolved path is inside the root
        try:
            resolved.relative_to(self.root.resolve())
        except ValueError:
            raise ValueError(f"Path {relative_path!r} escapes the capability root")
        return resolved

    def _check_not_special(self, path: Path) -> None:
        """Raise ValueError for device/special files."""
        if path.exists():
            st = path.stat()
            mode = st.st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise ValueError(f"Path {path} is not a regular file or directory")


def create_filesystem_capability(
    root_path: Path | str,
    *,
    allow_overwrite: bool = False,
    max_read_bytes: int = _MAX_FILE_READ_BYTES,
    max_write_bytes: int = _MAX_FILE_WRITE_BYTES,
    max_list_entries: int = _MAX_LIST_ENTRIES,
) -> str:
    """
    Create and register a FilesystemCapability for *root_path*.
    Returns the root_id (opaque handle).
    """
    root = Path(root_path).resolve()
    if not root.is_dir():
        raise ValueError(f"Capability root must be an existing directory: {root}")
    root_id = "fscap-" + secrets.token_hex(16)
    cap = FilesystemCapability(
        root=root,
        root_id=root_id,
        max_read_bytes=max_read_bytes,
        max_write_bytes=max_write_bytes,
        max_list_entries=max_list_entries,
        allow_overwrite=allow_overwrite,
    )
    _CAPABILITY_REGISTRY[root_id] = cap
    return root_id


def _get_capability(root_id: str) -> FilesystemCapability:
    cap = _CAPABILITY_REGISTRY.get(root_id)
    if cap is None:
        raise ValueError(f"Unknown filesystem capability root_id: {root_id!r}")
    return cap


# ── Scoped filesystem tools (governed production lane) ────────────────────────


def scoped_read_text_file(root_id: str, relative_path: str) -> str:
    """Read a file within a capability root. Governed production lane."""
    cap = _get_capability(root_id)
    resolved = cap._safe_resolve(relative_path)
    cap._check_not_special(resolved)
    if not resolved.exists() or not resolved.is_file():
        raise FileNotFoundError(f"No such file: {relative_path!r} in root {root_id!r}")
    size = resolved.stat().st_size
    if size > cap.max_read_bytes:
        raise ValueError(
            f"File {relative_path!r} size {size} exceeds read cap {cap.max_read_bytes}"
        )
    return resolved.read_text(encoding="utf-8")


def scoped_write_json_file(
    root_id: str,
    relative_path: str,
    data: Any,
    *,
    overwrite: bool | None = None,
) -> str:
    """
    Write *data* as finite canonical JSON inside a capability root.
    Uses a temp file + fsync + atomic rename.
    Governed production lane; deny overwrite by default.
    """
    cap = _get_capability(root_id)
    resolved = cap._safe_resolve(relative_path)
    cap._check_not_special(resolved)

    effective_overwrite = overwrite if overwrite is not None else cap.allow_overwrite
    if not effective_overwrite and resolved.exists():
        raise FileExistsError(f"File {relative_path!r} already exists and overwrite is denied")

    # Finite canonical JSON (rejects NaN/Infinity, cycles, non-string keys)
    payload_bytes = canonical_json(data)
    if len(payload_bytes) > cap.max_write_bytes:
        raise ValueError(
            f"JSON payload {len(payload_bytes)} bytes exceeds write cap {cap.max_write_bytes}"
        )

    # Write to temp file inside the approved root, then atomic rename
    parent = resolved.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path_str, resolved)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except OSError:
            pass  # best-effort temp file cleanup; original exception takes priority
        raise

    # Verify digest postcondition
    written = resolved.read_bytes()
    expected_digest = hashlib.sha256(payload_bytes).hexdigest()
    actual_digest = hashlib.sha256(written).hexdigest()
    if actual_digest != expected_digest:
        raise RuntimeError(f"Write postcondition failed: digest mismatch for {relative_path!r}")

    return relative_path


def scoped_list_directory(root_id: str, relative_path: str) -> list[str]:
    """List files in a directory within a capability root. Governed production lane."""
    cap = _get_capability(root_id)
    resolved = cap._safe_resolve(relative_path)
    cap._check_not_special(resolved)
    if not resolved.is_dir():
        raise NotADirectoryError(f"Not a directory: {relative_path!r} in root {root_id!r}")
    entries = sorted(str(item.name) for item in resolved.iterdir())
    if len(entries) > cap.max_list_entries:
        raise ValueError(
            f"Directory has {len(entries)} entries, exceeding cap {cap.max_list_entries}"
        )
    return entries


# ── Legacy tool implementations (development compatibility) ───────────────────


def echo_text(text: str) -> str:
    """Return text unchanged. Useful for testing closure."""
    validate_kwargs(_SPEC_ECHO, {"text": text})
    return text


def read_text_file(path: str) -> str:
    """Read a local text file and return its contents."""
    validate_kwargs(_SPEC_READ_FILE, {"path": path})
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"No such file: {p}")
    return p.read_text(encoding="utf-8")


def write_json_file(path: str, data: Any) -> str:
    """Write data as formatted JSON to path; returns the path string."""
    validate_kwargs(_SPEC_WRITE_JSON, {"path": path, "data": data})
    p = Path(path)
    p.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(p)


def list_directory(path: str) -> list[str]:
    """Return a sorted list of file names in directory path."""
    validate_kwargs(_SPEC_LIST_DIR, {"path": path})
    p = Path(path)
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {p}")
    return sorted(str(item.name) for item in p.iterdir())


# ── Legacy ToolSpec instances ─────────────────────────────────────────────────
_SPEC_ECHO = ToolSpec(
    name="echo_text",
    description="Return text unchanged. Useful for testing isomorphic closure.",
    required_kwargs=["text"],
    safety_tier="READ_ONLY",
)

_SPEC_READ_FILE = ToolSpec(
    name="read_text_file",
    description="Read a local text file and return its UTF-8 contents.",
    required_kwargs=["path"],
    safety_tier="READ_ONLY",
)

_SPEC_WRITE_JSON = ToolSpec(
    name="write_json_file",
    description="Write data as formatted JSON to a local file path.",
    required_kwargs=["path", "data"],
    safety_tier="WRITE_LOCAL",
)

_SPEC_LIST_DIR = ToolSpec(
    name="list_directory",
    description="Return a sorted list of file names in a directory.",
    required_kwargs=["path"],
    safety_tier="READ_ONLY",
)


# ── Any-JSON-value schema (used in governed write schema) ─────────────────────
_ANY_JSON_VALUE_SCHEMA: dict[str, Any] = {
    "anyOf": [
        {"type": "object", "additionalProperties": True},
        {"type": "array"},
        {"type": "string"},
        {"type": "integer"},
        {"type": "number"},
        {"type": "boolean"},
        {"type": "null"},
    ]
}

# ── ToolSpecV1 authority records ───────────────────────────────────────────────


def _desc_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_ECHO_DESC = "Return text unchanged. Useful for testing isomorphic closure."
_READ_DESC = "Read a UTF-8 text file within a server-approved capability root."
_WRITE_DESC = "Write JSON data atomically within a server-approved capability root."
_LIST_DESC = "List file names in a directory within a server-approved capability root."

TOOL_SPEC_V1_ECHO = ToolSpecV1(
    schema_version="1",
    tool_id="builtin.echo_text",
    tool_version="1.0.0",
    description_hash=_desc_hash(_ECHO_DESC),
    input_schema={
        "type": "object",
        "required": ["text"],
        "properties": {"text": {"type": "string"}},
        "additionalProperties": False,
    },
    output_schema={"type": "string"},
    capabilities=[],
    risk_class="LOW",
    required_principal_scopes=[],
    isolation_profile="in_process",
    worker_handler_id="builtin.echo_text.in_process",
    worker_build_identity="IN_PROCESS",
    default_deadline_ms=5_000,
    max_deadline_ms=30_000,
    max_input_bytes=64 * 1024,
    max_output_bytes=64 * 1024,
    reversibility="reversible",
    idempotency="idempotent",
    postcondition_validator_id="",
    postcondition_validator_version="",
    evidence_policy="digest_only",
    redaction_policy="default",
)

TOOL_SPEC_V1_READ_FILE = ToolSpecV1(
    schema_version="1",
    tool_id="builtin.read_text_file",
    tool_version="1.0.0",
    description_hash=_desc_hash(_READ_DESC),
    input_schema={
        "type": "object",
        "required": ["root_id", "relative_path"],
        "properties": {
            "root_id": {"type": "string"},
            "relative_path": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={"type": "string"},
    capabilities=["filesystem.read"],
    risk_class="LOW",
    required_principal_scopes=["filesystem.read"],
    isolation_profile="in_process",
    worker_handler_id="builtin.read_text_file.in_process",
    worker_build_identity="IN_PROCESS",
    default_deadline_ms=10_000,
    max_deadline_ms=60_000,
    max_input_bytes=4 * 1024,
    max_output_bytes=4 * 1024 * 1024,
    reversibility="reversible",
    idempotency="idempotent",
    postcondition_validator_id="",
    postcondition_validator_version="",
    evidence_policy="digest_only",
    redaction_policy="default",
)

TOOL_SPEC_V1_WRITE_JSON = ToolSpecV1(
    schema_version="1",
    tool_id="builtin.write_json_file",
    tool_version="1.0.0",
    description_hash=_desc_hash(_WRITE_DESC),
    input_schema={
        "type": "object",
        "required": ["root_id", "relative_path", "data"],
        "properties": {
            "root_id": {"type": "string"},
            "relative_path": {"type": "string"},
            "data": _ANY_JSON_VALUE_SCHEMA,
        },
        "additionalProperties": False,
    },
    output_schema={"type": "string"},
    capabilities=["filesystem.write"],
    risk_class="MEDIUM",
    required_principal_scopes=["filesystem.write"],
    isolation_profile="in_process",
    worker_handler_id="builtin.write_json_file.in_process",
    worker_build_identity="IN_PROCESS",
    default_deadline_ms=15_000,
    max_deadline_ms=60_000,
    max_input_bytes=4 * 1024 * 1024,
    max_output_bytes=1024,
    reversibility="irreversible",
    idempotency="non_idempotent",
    postcondition_validator_id="builtin.write_json_file.digest_check",
    postcondition_validator_version="1.0.0",
    evidence_policy="digest_only",
    redaction_policy="default",
)

TOOL_SPEC_V1_LIST_DIR = ToolSpecV1(
    schema_version="1",
    tool_id="builtin.list_directory",
    tool_version="1.0.0",
    description_hash=_desc_hash(_LIST_DESC),
    input_schema={
        "type": "object",
        "required": ["root_id", "relative_path"],
        "properties": {
            "root_id": {"type": "string"},
            "relative_path": {"type": "string"},
        },
        "additionalProperties": False,
    },
    output_schema={
        "type": "array",
        "items": {"type": "string"},
    },
    capabilities=["filesystem.read"],
    risk_class="LOW",
    required_principal_scopes=["filesystem.read"],
    isolation_profile="in_process",
    worker_handler_id="builtin.list_directory.in_process",
    worker_build_identity="IN_PROCESS",
    default_deadline_ms=10_000,
    max_deadline_ms=60_000,
    max_input_bytes=4 * 1024,
    max_output_bytes=256 * 1024,
    reversibility="reversible",
    idempotency="idempotent",
    postcondition_validator_id="",
    postcondition_validator_version="",
    evidence_policy="digest_only",
    redaction_policy="default",
)

#: Map from governed tool_id to (ToolSpecV1, governed_callable)
GOVERNED_TOOL_REGISTRY: dict[str, tuple[ToolSpecV1, Callable]] = {
    "builtin.echo_text": (TOOL_SPEC_V1_ECHO, echo_text),
    "builtin.read_text_file": (TOOL_SPEC_V1_READ_FILE, scoped_read_text_file),
    "builtin.write_json_file": (TOOL_SPEC_V1_WRITE_JSON, scoped_write_json_file),
    "builtin.list_directory": (TOOL_SPEC_V1_LIST_DIR, scoped_list_directory),
}


# ── write_json_file postcondition validator ────────────────────────────────────


def _write_json_file_postcondition(kwargs: Any, output: Any, metadata: Any) -> None:
    """
    Verify that the file written by scoped_write_json_file has the expected digest.
    Operates on bounded evidence (root_id, relative_path, data) from kwargs; never
    stores or logs raw file bodies.
    """
    root_id = kwargs.get("root_id", "") if isinstance(kwargs, dict) else ""
    relative_path = kwargs.get("relative_path", "") if isinstance(kwargs, dict) else ""
    data = kwargs.get("data") if isinstance(kwargs, dict) else None
    if not isinstance(root_id, str) or not isinstance(relative_path, str):
        raise PostconditionFailedError("write_json_file postcondition: invalid kwargs types")
    try:
        cap = _get_capability(root_id)
        resolved = cap._safe_resolve(relative_path)
    except Exception as exc:
        raise PostconditionFailedError(
            f"write_json_file postcondition: capability resolution failed: {exc}"
        ) from exc
    if not resolved.is_file():
        raise PostconditionFailedError(
            f"write_json_file postcondition: file {relative_path!r} not found after write"
        )
    expected_bytes = canonical_json(data)
    written_bytes = resolved.read_bytes()
    if written_bytes != expected_bytes:
        raise PostconditionFailedError(
            f"write_json_file postcondition: digest mismatch for {relative_path!r}"
        )


#: Module-level postcondition validator registry populated with built-in validators.
BUILTIN_POSTCONDITION_VALIDATORS = PostconditionValidatorRegistry()
BUILTIN_POSTCONDITION_VALIDATORS.register(
    "builtin.write_json_file.digest_check",
    "1.0.0",
    _write_json_file_postcondition,
)


# ── Tool registry (legacy compatibility) ─────────────────────────────────────
#
# TOOL_REGISTRY maps tool name → (callable, ToolSpec).
# Orchestrator.register_tool() accepts only the callable; the ToolSpec is
# available here for schema validation and LLM context injection.
#
TOOL_REGISTRY: dict[str, tuple[Callable, ToolSpec]] = {
    "echo_text": (echo_text, _SPEC_ECHO),
    "read_text_file": (read_text_file, _SPEC_READ_FILE),
    "write_json_file": (write_json_file, _SPEC_WRITE_JSON),
    "list_directory": (list_directory, _SPEC_LIST_DIR),
}


def register_all(orchestrator: Any) -> None:
    """
    Register all built-in tools with an Orchestrator instance.

    If *orchestrator* has a ``tool_registry`` attribute that is a
    ``ToolRegistry`` instance, also registers ToolSpecV1 authority entries
    and governed callables under their ``tool_id`` key (governed mode).
    The legacy callable names (e.g. ``"echo_text"``) are always registered
    for backward compatibility.

    Usage
    -----
        from sovereign_claw import Orchestrator, TaskManifold
        from sovereign_claw.tools_basic import register_all
        orch = Orchestrator(llm_backend=my_llm)
        register_all(orch)
    """
    # Always register legacy callables (development compatibility lane)
    for name, (fn, _spec) in TOOL_REGISTRY.items():
        orchestrator.register_tool(name, fn)

    # Governed path: register ToolSpecV1 entries and governed handlers
    tool_registry = getattr(orchestrator, "tool_registry", None)
    if isinstance(tool_registry, ToolRegistry):
        for spec_v1, governed_fn in GOVERNED_TOOL_REGISTRY.values():
            entry = make_registry_entry(spec_v1)
            tool_registry.register(entry)
            # Register governed callable under an immutable handler binding keyed by the
            # exact worker_handler_id from the ToolSpec — dispatch resolves by this ID.
            orchestrator.register_governed_handler(spec_v1.worker_handler_id, governed_fn)

    # Governed path: register built-in postcondition validators if the orchestrator
    # has a PostconditionValidatorRegistry configured.
    pv_registry = getattr(orchestrator, "postcondition_validator_registry", None)
    if isinstance(pv_registry, PostconditionValidatorRegistry):
        for (vid, version), fn in BUILTIN_POSTCONDITION_VALIDATORS._validators.items():
            pv_registry.register(vid, version, fn)


def tool_descriptions() -> list[dict[str, Any]]:
    """
    Return a list of tool descriptor dicts suitable for LLM context injection.
    Each dict contains: name, description, required_kwargs, safety_tier.
    """
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "required_kwargs": spec.required_kwargs,
            "safety_tier": spec.safety_tier,
        }
        for _fn, spec in TOOL_REGISTRY.values()
    ]


__all__ = [
    "BUILTIN_POSTCONDITION_VALIDATORS",
    "GOVERNED_TOOL_REGISTRY",
    "TOOL_REGISTRY",
    "TOOL_SPEC_V1_ECHO",
    "TOOL_SPEC_V1_LIST_DIR",
    "TOOL_SPEC_V1_READ_FILE",
    "TOOL_SPEC_V1_WRITE_JSON",
    "FilesystemCapability",
    "SafetyTier",
    "ToolSpec",
    "create_filesystem_capability",
    "echo_text",
    "list_directory",
    "read_text_file",
    "register_all",
    "scoped_list_directory",
    "scoped_read_text_file",
    "scoped_write_json_file",
    "tool_descriptions",
    "validate_kwargs",
    "write_json_file",
]
