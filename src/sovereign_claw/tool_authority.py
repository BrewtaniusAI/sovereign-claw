"""
tool_authority.py — ToolSpecV1 Authority Contract (#40)
========================================================
Implements the immutable, versioned tool authority contract layer described in
docs/TOOLSPEC_AUTHORITY.md.

Key design principles
---------------------
* ToolSpecV1 is frozen/immutable; canonical bytes use deterministic finite JSON
  (UTF-8, sorted keys, compact separators, no NaN/Infinity).
* tool_contract_hash = SHA-256(canonical_json(authority_fields)).
* ToolRegistry binds exactly (ToolSpecV1, tool_contract_hash, worker_handler_id)
  and rejects duplicate tool_id with changed contract, handler substitution, and
  invalid schemas.
* Input/output schema validation is bounded, deterministic, and does NOT execute
  code or resolve remote schemas.
* Action digest binds tool_id + tool_contract_hash + canonical args +
  policy/config/principal identity; never derived from inspect.signature().
* Raw callable registration is preserved for backward compatibility but cannot
  mint a production action digest.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: str = "1"
ACTION_VERSION: str = "1"

# Reasonable recursion limit for nested schemas / values
_MAX_DEPTH: int = 32
# Maximum size of canonical JSON bytes for a ToolSpec
_MAX_SPEC_BYTES: int = 256 * 1024  # 256 KiB


# ---------------------------------------------------------------------------
# Stable error classes
# ---------------------------------------------------------------------------


class ToolAuthorityError(Exception):
    """Base class for all tool authority errors."""

    error_code: str = "TOOL_AUTHORITY_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.error_code = code or self.__class__.error_code


class UnknownToolError(ToolAuthorityError):
    """Tool ID not found in registry."""

    error_code = "UNKNOWN_TOOL"


class ToolContractChangedError(ToolAuthorityError):
    """Registry entry differs from the snapshot used during preview."""

    error_code = "TOOL_CONTRACT_CHANGED"


class InputSchemaInvalidError(ToolAuthorityError):
    """Provided arguments fail input_schema validation."""

    error_code = "INPUT_SCHEMA_INVALID"


class OutputSchemaInvalidError(ToolAuthorityError):
    """Handler result fails output_schema validation."""

    error_code = "OUTPUT_SCHEMA_INVALID"


class DuplicateToolRegistrationError(ToolAuthorityError):
    """Attempt to register a tool_id that already exists with a different contract."""

    error_code = "DUPLICATE_TOOL_ID"


class HandlerSubstitutionError(ToolAuthorityError):
    """Attempt to substitute a handler under an existing immutable contract."""

    error_code = "HANDLER_SUBSTITUTION_REJECTED"


class InvalidSchemaError(ToolAuthorityError):
    """The supplied schema definition is structurally invalid."""

    error_code = "INVALID_SCHEMA"


class InvalidSpecFieldError(ToolAuthorityError):
    """An authority field on the ToolSpecV1 has an invalid value."""

    error_code = "INVALID_SPEC_FIELD"


class HashMismatchError(ToolAuthorityError):
    """A supplied tool_contract_hash does not match the recomputed canonical hash."""

    error_code = "HASH_MISMATCH"


class RawCallableAuthorityError(ToolAuthorityError):
    """A raw callable cannot mint a production action digest."""

    error_code = "RAW_CALLABLE_NO_PRODUCTION_AUTHORITY"


class ApprovedActionMismatchError(ToolAuthorityError):
    """Action digest does not match the approved snapshot."""

    error_code = "APPROVED_ACTION_MISMATCH"


class CyclicValueError(ToolAuthorityError):
    """A cyclic reference was detected in a value or schema structure."""

    error_code = "CYCLIC_VALUE"


class NonStringKeyError(ToolAuthorityError):
    """A mapping contains a non-string key, which is not permitted."""

    error_code = "NON_STRING_KEY"



class UnsupportedValueTypeError(ToolAuthorityError):
    """A value of an unsupported type was encountered during serialization."""

    error_code = "UNSUPPORTED_VALUE_TYPE"


class PostconditionFailedError(ToolAuthorityError):
    """Declared postcondition verification failed for a governed tool execution."""

    error_code = "POSTCONDITION_FAILED"


class MissingPostconditionValidatorError(ToolAuthorityError):
    """A declared postcondition validator was not found in the registry."""

    error_code = "MISSING_POSTCONDITION_VALIDATOR"


class EvidencePersistenceFailedError(ToolAuthorityError):
    """Authority evidence could not be persisted after tool actuation."""

    error_code = "EVIDENCE_PERSISTENCE_FAILED"


# ---------------------------------------------------------------------------
# Canonical JSON helpers
# ---------------------------------------------------------------------------


def _is_finite_scalar(value: Any) -> bool:
    """Return True for JSON-serialisable scalars with no NaN/Infinity."""
    if isinstance(value, bool):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return isinstance(value, (int, str)) or value is None


def _check_structure(
    value: Any, depth: int = 0, _seen: frozenset[int] | None = None
) -> None:
    """
    Recursively check a value for:
    - Exceeding _MAX_DEPTH nesting
    - Cyclic references
    - Non-string dict keys
    - Unsupported types (e.g. set, tuple, bytes)

    Does NOT reject non-finite floats — that is handled separately
    by ``_check_finite`` (for canonical_json) and inline in ``validate_value``
    (for schema validation), so the caller gets the right error message.
    """
    if depth > _MAX_DEPTH:
        raise ValueError(f"Value exceeds maximum nesting depth {_MAX_DEPTH}")
    if isinstance(value, (dict, list)):
        seen = _seen if _seen is not None else frozenset()
        vid = id(value)
        if vid in seen:
            raise CyclicValueError("Cyclic reference detected in value")
        seen = seen | {vid}
        if isinstance(value, dict):
            for k, v in value.items():
                if not isinstance(k, str):
                    raise NonStringKeyError(
                        f"Mapping keys must be str; got {type(k).__name__!r}"
                    )
                _check_structure(v, depth + 1, seen)
        else:
            for item in value:
                _check_structure(item, depth + 1, seen)
    elif value is None or isinstance(value, (bool, int, float, str)):
        pass
    else:
        raise UnsupportedValueTypeError(
            f"Unsupported value type {type(value).__name__!r} for canonical serialization"
        )


def _check_finite(value: Any, depth: int = 0) -> None:
    """Raise ValueError if any float is NaN or Infinity (canonical_json guard)."""
    if depth > _MAX_DEPTH:
        return  # depth already checked by _check_structure
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Non-finite float value not permitted: {value!r}")
    if isinstance(value, dict):
        for v in value.values():
            _check_finite(v, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_finite(item, depth + 1)


def _check_depth(value: Any, depth: int = 0) -> None:
    """Raise ValueError if nested depth exceeds _MAX_DEPTH or structural checks fail."""
    _check_structure(value, depth)


def _validate_no_nonfinite(value: Any, depth: int = 0) -> None:
    """Validate *value* for use in canonical JSON.

    Raises
    ------
    CyclicValueError
        If a cyclic reference is detected.
    NonStringKeyError
        If a mapping has a non-string key.
    UnsupportedValueTypeError
        If an unsupported type (e.g. set, bytes) is encountered.
    ValueError
        If any float is NaN or Infinity.
    """
    _check_structure(value, depth)
    _check_finite(value, depth)


def canonical_json(obj: Any) -> bytes:
    """
    Produce deterministic finite canonical JSON bytes.

    Rules:
    - UTF-8 encoded
    - Keys sorted recursively
    - Compact separators (",", ":")
    - No NaN or Infinity floats permitted
    - No cyclic references
    - All mapping keys must be str
    - Only JSON-compatible types (dict/list/str/int/float/bool/None)
    - ensure_ascii=False (Unicode code points preserved)
    """
    _check_structure(obj)
    _check_finite(obj)

    def _sorted(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: _sorted(v) for k, v in sorted(o.items())}
        if isinstance(o, list):
            return [_sorted(item) for item in o]
        return o

    return json.dumps(
        _sorted(obj),
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=True,  # belt-and-suspenders
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Schema validation (bounded, deterministic, no remote resolvers)
# ---------------------------------------------------------------------------

# Supported JSON-Schema-shaped types
_SCALAR_TYPES = frozenset({"string", "integer", "number", "boolean", "null"})
_ALL_TYPES = _SCALAR_TYPES | frozenset({"object", "array"})


def _validate_schema_structure(
    schema: Any, path: str = "schema", _depth: int = 0
) -> None:
    """
    Validate that a schema definition is structurally well-formed.
    Raises InvalidSchemaError for structural problems.
    """
    if _depth > _MAX_DEPTH:
        raise InvalidSchemaError(f"{path}: schema nesting exceeds maximum depth {_MAX_DEPTH}")
    if not isinstance(schema, dict):
        raise InvalidSchemaError(f"{path}: schema must be a dict, got {type(schema).__name__}")
    type_val = schema.get("type")

    # Validate combinators: anyOf/oneOf must be non-empty lists of dicts
    for combinator in ("anyOf", "oneOf"):
        cval = schema.get(combinator)
        if cval is not None:
            if not isinstance(cval, list) or len(cval) == 0:
                raise InvalidSchemaError(
                    f"{path}.{combinator} must be a non-empty list of schema dicts"
                )
            for i, sub in enumerate(cval):
                _validate_schema_structure(sub, f"{path}.{combinator}[{i}]", _depth + 1)

    if type_val is None:
        # Allow schemas with only "enum", "anyOf", or "oneOf"
        if (
            "enum" not in schema
            and "anyOf" not in schema
            and "oneOf" not in schema
        ):
            raise InvalidSchemaError(
                f"{path}: schema must have 'type', 'enum', 'anyOf', or 'oneOf'"
            )
    elif type_val not in _ALL_TYPES:
        raise InvalidSchemaError(
            f"{path}: unsupported type {type_val!r}; must be one of {sorted(_ALL_TYPES)}"
        )

    if type_val == "object":
        props = schema.get("properties", {})
        if not isinstance(props, dict):
            raise InvalidSchemaError(f"{path}.properties must be a dict")
        for prop_name, prop_schema in props.items():
            _validate_schema_structure(
                prop_schema, f"{path}.properties.{prop_name}", _depth + 1
            )
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise InvalidSchemaError(f"{path}.required must be a list")
        for r in required:
            if not isinstance(r, str):
                raise InvalidSchemaError(f"{path}.required items must be strings")
        # additionalProperties must be bool or a schema dict
        ap = schema.get("additionalProperties", False)
        if not isinstance(ap, (bool, dict)):
            raise InvalidSchemaError(
                f"{path}.additionalProperties must be bool or schema dict"
            )
        if isinstance(ap, dict):
            _validate_schema_structure(ap, f"{path}.additionalProperties", _depth + 1)

    if type_val == "array":
        items = schema.get("items")
        if items is not None:
            _validate_schema_structure(items, f"{path}.items", _depth + 1)
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if min_items is not None and not isinstance(min_items, int):
            raise InvalidSchemaError(f"{path}.minItems must be int")
        if max_items is not None and not isinstance(max_items, int):
            raise InvalidSchemaError(f"{path}.maxItems must be int")

    if type_val == "string":
        for kw in ("minLength", "maxLength"):
            v = schema.get(kw)
            if v is not None and not isinstance(v, int):
                raise InvalidSchemaError(f"{path}.{kw} must be int")

    if type_val in ("integer", "number"):
        for kw in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            v = schema.get(kw)
            if v is not None:
                if isinstance(v, float) and not math.isfinite(v):
                    raise InvalidSchemaError(f"{path}.{kw} must be finite")
                if not isinstance(v, (int, float)):
                    raise InvalidSchemaError(f"{path}.{kw} must be numeric")

    # enum validation
    enum_vals = schema.get("enum")
    if enum_vals is not None and (not isinstance(enum_vals, list) or len(enum_vals) == 0):
        raise InvalidSchemaError(f"{path}.enum must be a non-empty list")


def validate_value(
    value: Any, schema: Any, path: str = "value", depth: int = 0
) -> None:
    """
    Validate *value* against a JSON-Schema-shaped *schema*.
    Raises InputSchemaInvalidError (or OutputSchemaInvalidError) on mismatch.
    Unknown object keys are denied by default (additionalProperties defaults to False).
    The caller should wrap in the appropriate error class.
    """
    if depth > _MAX_DEPTH:
        raise ValueError(f"Value exceeds maximum nesting depth {_MAX_DEPTH}")

    if not isinstance(schema, dict):
        raise InvalidSchemaError(f"{path}: schema must be a dict")

    # enum short-circuit
    enum_vals = schema.get("enum")
    if enum_vals is not None:
        if value not in enum_vals:
            raise ValueError(f"{path}: value {value!r} not in enum {enum_vals!r}")
        return

    # anyOf — at least one match
    if "anyOf" in schema:
        sub_schemas = schema.get("anyOf")
        if not isinstance(sub_schemas, list) or len(sub_schemas) == 0:
            raise InvalidSchemaError(f"{path}: anyOf must be a non-empty list")
        errors: list[str] = []
        for sub in sub_schemas:
            try:
                validate_value(value, sub, path, depth)
                return
            except (ValueError, InvalidSchemaError) as exc:
                errors.append(str(exc))
        raise ValueError(f"{path}: value does not match any anyOf sub-schema; errors: {errors!r}")

    # oneOf — exactly one match
    if "oneOf" in schema:
        sub_schemas = schema.get("oneOf")
        if not isinstance(sub_schemas, list) or len(sub_schemas) == 0:
            raise InvalidSchemaError(f"{path}: oneOf must be a non-empty list")
        match_count = 0
        errors = []
        for sub in sub_schemas:
            try:
                validate_value(value, sub, path, depth)
                match_count += 1
            except (ValueError, InvalidSchemaError) as exc:
                errors.append(str(exc))
        if match_count != 1:
            raise ValueError(
                f"{path}: value must match exactly one oneOf sub-schema, "
                f"matched {match_count}; errors: {errors!r}"
            )
        return

    type_val = schema.get("type")
    if type_val is None:
        # No type constraint; pass through (enum handled above)
        return

    # Type check
    if type_val == "null":
        if value is not None:
            raise ValueError(f"{path}: expected null, got {type(value).__name__}")
        return
    if type_val == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{path}: expected boolean, got {type(value).__name__}")
        return
    if type_val == "integer":
        # int but not bool
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path}: expected integer, got {type(value).__name__}")
        _check_numeric_bounds(value, schema, path)
        return
    if type_val == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: expected number, got {type(value).__name__}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path}: non-finite float not permitted")
        _check_numeric_bounds(value, schema, path)
        return
    if type_val == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path}: expected string, got {type(value).__name__}")
        min_len = schema.get("minLength")
        max_len = schema.get("maxLength")
        if min_len is not None and len(value) < min_len:
            raise ValueError(f"{path}: string length {len(value)} < minLength {min_len}")
        if max_len is not None and len(value) > max_len:
            raise ValueError(f"{path}: string length {len(value)} > maxLength {max_len}")
        return
    if type_val == "array":
        if not isinstance(value, list):
            raise ValueError(f"{path}: expected array, got {type(value).__name__}")
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if min_items is not None and len(value) < min_items:
            raise ValueError(f"{path}: array length {len(value)} < minItems {min_items}")
        if max_items is not None and len(value) > max_items:
            raise ValueError(f"{path}: array length {len(value)} > maxItems {max_items}")
        items_schema = schema.get("items")
        if items_schema is not None:
            for i, item in enumerate(value):
                validate_value(item, items_schema, f"{path}[{i}]", depth + 1)
        return
    if type_val == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path}: expected object, got {type(value).__name__}")
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        # Default additionalProperties to False (strict)
        additional_props = schema.get("additionalProperties", False)

        for req_key in required:
            if req_key not in value:
                raise ValueError(f"{path}: missing required property {req_key!r}")

        for key, val in value.items():
            if key in properties:
                validate_value(val, properties[key], f"{path}.{key}", depth + 1)
            else:
                if additional_props is False:
                    raise ValueError(
                        f"{path}: unknown property {key!r} not allowed "
                        f"(additionalProperties=false)"
                    )
                if isinstance(additional_props, dict):
                    validate_value(val, additional_props, f"{path}.{key}", depth + 1)
        return


def _check_numeric_bounds(value: Any, schema: dict, path: str) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    exc_min = schema.get("exclusiveMinimum")
    exc_max = schema.get("exclusiveMaximum")
    if minimum is not None and value < minimum:
        raise ValueError(f"{path}: {value} < minimum {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{path}: {value} > maximum {maximum}")
    if exc_min is not None and value <= exc_min:
        raise ValueError(f"{path}: {value} <= exclusiveMinimum {exc_min}")
    if exc_max is not None and value >= exc_max:
        raise ValueError(f"{path}: {value} >= exclusiveMaximum {exc_max}")


def validate_input(value: Any, schema: Any) -> None:
    """Validate value against schema, raising InputSchemaInvalidError on failure."""
    _validate_schema_structure(schema, "input_schema")
    try:
        _check_depth(value)
        validate_value(value, schema, "input")
    except (ValueError, InvalidSchemaError) as exc:
        raise InputSchemaInvalidError(str(exc)) from exc


def validate_output(value: Any, schema: Any) -> None:
    """Validate value against schema, raising OutputSchemaInvalidError on failure."""
    _validate_schema_structure(schema, "output_schema")
    try:
        validate_value(value, schema, "output")
    except (ValueError, InvalidSchemaError) as exc:
        raise OutputSchemaInvalidError(str(exc)) from exc


# ---------------------------------------------------------------------------
# ToolSpecV1
# ---------------------------------------------------------------------------

# Allowed isolation profiles
_ISOLATION_PROFILES = frozenset({
    "in_process",
    "subprocess",
    "container",
    "network_isolated_container",
    "sandbox",
})

# Allowed risk classes
_RISK_CLASSES = frozenset({
    "LOW",
    "MEDIUM",
    "HIGH",
    "CRITICAL",
})

# Allowed reversibility / idempotency values
_REVERSIBILITY = frozenset({"reversible", "irreversible", "partially_reversible"})
_IDEMPOTENCY = frozenset({"idempotent", "non_idempotent", "at_most_once"})


@dataclass(frozen=True)
class ToolSpecV1:
    """
    Immutable, versioned authority specification for a tool.

    All fields contribute to the canonical JSON and therefore to
    tool_contract_hash.  Use ToolSpecV1.compute_contract_hash() to derive the
    binding hash.
    """

    schema_version: str
    tool_id: str
    tool_version: str
    description_hash: str  # SHA-256 of the display description; not display text
    input_schema: Any  # JSON-Schema-shaped dict
    output_schema: Any  # JSON-Schema-shaped dict
    capabilities: list[str]
    risk_class: str
    required_principal_scopes: list[str]
    isolation_profile: str
    worker_handler_id: str
    worker_build_identity: str  # image digest / commit SHA / "IN_PROCESS" etc.
    default_deadline_ms: int
    max_deadline_ms: int
    max_input_bytes: int
    max_output_bytes: int
    reversibility: str
    idempotency: str
    postcondition_validator_id: str  # "" means none declared
    postcondition_validator_version: str
    evidence_policy: str  # e.g. "digest_only", "full", "none"
    redaction_policy: str  # e.g. "default", "none"

    # Extra authority fields — optional, extend as needed
    extra_authority: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate required string fields
        for attr in (
            "schema_version",
            "tool_id",
            "tool_version",
            "description_hash",
            "worker_handler_id",
            "worker_build_identity",
            "evidence_policy",
            "redaction_policy",
            "postcondition_validator_id",
            "postcondition_validator_version",
        ):
            val = getattr(self, attr)
            if not isinstance(val, str):
                raise InvalidSpecFieldError(f"{attr} must be a str")

        if not self.tool_id:
            raise InvalidSpecFieldError("tool_id must not be empty")
        if not self.worker_handler_id:
            raise InvalidSpecFieldError("worker_handler_id must not be empty")

        if self.risk_class not in _RISK_CLASSES:
            raise InvalidSpecFieldError(
                f"risk_class must be one of {sorted(_RISK_CLASSES)}, got {self.risk_class!r}"
            )
        if self.isolation_profile not in _ISOLATION_PROFILES:
            raise InvalidSpecFieldError(
                f"isolation_profile must be one of {sorted(_ISOLATION_PROFILES)}, "
                f"got {self.isolation_profile!r}"
            )
        if self.reversibility not in _REVERSIBILITY:
            raise InvalidSpecFieldError(
                f"reversibility must be one of {sorted(_REVERSIBILITY)}, "
                f"got {self.reversibility!r}"
            )
        if self.idempotency not in _IDEMPOTENCY:
            raise InvalidSpecFieldError(
                f"idempotency must be one of {sorted(_IDEMPOTENCY)}, "
                f"got {self.idempotency!r}"
            )
        for attr in (
            "default_deadline_ms",
            "max_deadline_ms",
            "max_input_bytes",
            "max_output_bytes",
        ):
            val = getattr(self, attr)
            if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
                raise InvalidSpecFieldError(f"{attr} must be a positive integer")
        if self.default_deadline_ms > self.max_deadline_ms:
            raise InvalidSpecFieldError(
                "default_deadline_ms must be <= max_deadline_ms"
            )
        if not isinstance(self.capabilities, list):
            raise InvalidSpecFieldError("capabilities must be a list")
        if not isinstance(self.required_principal_scopes, list):
            raise InvalidSpecFieldError("required_principal_scopes must be a list")
        if not isinstance(self.extra_authority, dict):
            raise InvalidSpecFieldError("extra_authority must be a dict")

        # Validate schemas structurally
        try:
            _validate_schema_structure(self.input_schema, "input_schema")
        except InvalidSchemaError as exc:
            raise InvalidSpecFieldError(f"input_schema invalid: {exc}") from exc
        try:
            _validate_schema_structure(self.output_schema, "output_schema")
        except InvalidSchemaError as exc:
            raise InvalidSpecFieldError(f"output_schema invalid: {exc}") from exc

        # Check for non-finite floats in extra_authority
        try:
            _validate_no_nonfinite(self.extra_authority)
        except ValueError as exc:
            raise InvalidSpecFieldError(f"extra_authority: {exc}") from exc

    def _authority_dict(self) -> dict:
        """Return the ordered dict of authority fields for canonical JSON."""
        return {
            "schema_version": self.schema_version,
            "tool_id": self.tool_id,
            "tool_version": self.tool_version,
            "description_hash": self.description_hash,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
            "capabilities": sorted(self.capabilities),
            "risk_class": self.risk_class,
            "required_principal_scopes": sorted(self.required_principal_scopes),
            "isolation_profile": self.isolation_profile,
            "worker_handler_id": self.worker_handler_id,
            "worker_build_identity": self.worker_build_identity,
            "default_deadline_ms": self.default_deadline_ms,
            "max_deadline_ms": self.max_deadline_ms,
            "max_input_bytes": self.max_input_bytes,
            "max_output_bytes": self.max_output_bytes,
            "reversibility": self.reversibility,
            "idempotency": self.idempotency,
            "postcondition_validator_id": self.postcondition_validator_id,
            "postcondition_validator_version": self.postcondition_validator_version,
            "evidence_policy": self.evidence_policy,
            "redaction_policy": self.redaction_policy,
            "extra_authority": self.extra_authority,
        }

    def canonical_bytes(self) -> bytes:
        """Return deterministic canonical JSON bytes for this spec."""
        data = canonical_json(self._authority_dict())
        if len(data) > _MAX_SPEC_BYTES:
            raise InvalidSpecFieldError(
                f"ToolSpecV1 canonical JSON exceeds {_MAX_SPEC_BYTES} bytes"
            )
        return data

    def compute_contract_hash(self) -> str:
        """Return SHA-256 hex of canonical_bytes()."""
        return sha256_hex(self.canonical_bytes())


# ---------------------------------------------------------------------------
# ToolRegistryEntry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolRegistryEntry:
    """
    Immutable binding: (ToolSpecV1, tool_contract_hash, worker_handler_id).
    Optionally carries a trusted_execution_class for in-process tools.
    """

    spec: ToolSpecV1
    tool_contract_hash: str
    worker_handler_id: str
    trusted_execution_class: str | None = None

    def __post_init__(self) -> None:
        computed = self.spec.compute_contract_hash()
        if self.tool_contract_hash != computed:
            raise HashMismatchError(
                f"Supplied tool_contract_hash {self.tool_contract_hash!r} "
                f"does not match recomputed hash {computed!r}"
            )
        if self.worker_handler_id != self.spec.worker_handler_id:
            raise HandlerSubstitutionError(
                f"worker_handler_id {self.worker_handler_id!r} does not match "
                f"spec.worker_handler_id {self.spec.worker_handler_id!r}"
            )


def make_registry_entry(
    spec: ToolSpecV1,
    trusted_execution_class: str | None = None,
) -> ToolRegistryEntry:
    """Convenience constructor: compute hash automatically."""
    return ToolRegistryEntry(
        spec=spec,
        tool_contract_hash=spec.compute_contract_hash(),
        worker_handler_id=spec.worker_handler_id,
        trusted_execution_class=trusted_execution_class,
    )


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------


class ToolRegistry:
    """
    Governed registry of immutable ToolRegistryEntry records.

    Enforces:
    - No duplicate tool_id with a changed contract.
    - Handler substitution under the same tool name/hash is rejected.
    - Invalid schemas or authority fields are rejected at registration.
    """

    def __init__(self) -> None:
        self._entries: dict[str, ToolRegistryEntry] = {}

    def register(self, entry: ToolRegistryEntry) -> None:
        """
        Register a ToolRegistryEntry.  Raises if:
        - tool_id already registered with a different contract hash
        - tool_id already registered with a different handler
        """
        tool_id = entry.spec.tool_id
        existing = self._entries.get(tool_id)
        if existing is not None:
            if existing.tool_contract_hash != entry.tool_contract_hash:
                raise DuplicateToolRegistrationError(
                    f"tool_id {tool_id!r} already registered with contract hash "
                    f"{existing.tool_contract_hash!r}; new hash "
                    f"{entry.tool_contract_hash!r} requires an explicit version change"
                )
            if existing.worker_handler_id != entry.worker_handler_id:
                raise HandlerSubstitutionError(
                    f"tool_id {tool_id!r}: handler substitution rejected "
                    f"(existing={existing.worker_handler_id!r}, "
                    f"new={entry.worker_handler_id!r})"
                )
            # Idempotent re-registration of identical entry is allowed
            return
        self._entries[tool_id] = entry

    def get(self, tool_id: str) -> ToolRegistryEntry:
        """Return the entry for tool_id or raise UnknownToolError."""
        entry = self._entries.get(tool_id)
        if entry is None:
            raise UnknownToolError(f"Unknown tool: {tool_id!r}")
        return entry

    def snapshot(self) -> dict[str, ToolRegistryEntry]:
        """Return a shallow copy of the current registry state."""
        return dict(self._entries)

    def snapshot_hash(self) -> str:
        """Return a deterministic hash of the full registry state."""
        payload = canonical_json(
            {tid: entry.tool_contract_hash for tid, entry in sorted(self._entries.items())}
        )
        return sha256_hex(payload)

    def all_tool_ids(self) -> list[str]:
        return sorted(self._entries.keys())


# ---------------------------------------------------------------------------
# Canonical args helper
# ---------------------------------------------------------------------------


def canonicalize_args(
    args: dict[str, Any], input_schema: Any, max_input_bytes: int
) -> bytes:
    """
    Validate *args* against *input_schema* and return canonical JSON bytes.
    Raises InputSchemaInvalidError if validation fails.
    Raises InputSchemaInvalidError if the canonical representation exceeds
    *max_input_bytes*.
    """
    validate_input(args, input_schema)
    data = canonical_json(args)
    if len(data) > max_input_bytes:
        raise InputSchemaInvalidError(
            f"Canonical args size {len(data)} bytes exceeds "
            f"max_input_bytes {max_input_bytes}"
        )
    return data


# ---------------------------------------------------------------------------
# Action digest
# ---------------------------------------------------------------------------


def compute_action_digest(
    *,
    tool_id: str,
    tool_contract_hash: str,
    canonical_args_bytes: bytes,
    policy_bundle_hash: str,
    config_identity_hash: str,
    principal_identity: str,
) -> str:
    """
    Compute the deterministic action digest.

    Binds: tool_id + tool_contract_hash + canonical_args + policy/config/principal.
    Does NOT use inspect.signature() or any Python runtime attribute.

    Parameters
    ----------
    tool_id               : Stable tool identifier from the registry entry.
    tool_contract_hash    : SHA-256 hex of the ToolSpecV1 canonical bytes.
    canonical_args_bytes  : Output of canonicalize_args() — already validated.
    policy_bundle_hash    : Hash/identity of the policy bundle in effect.
    config_identity_hash  : Hash/identity of the runtime configuration.
    principal_identity    : Identity of the requesting principal/execution context.

    Returns
    -------
    SHA-256 hex string of the canonical action material.
    """
    material = canonical_json(
        {
            "action_version": ACTION_VERSION,
            "tool_id": tool_id,
            "tool_contract_hash": tool_contract_hash,
            "canonical_args": canonical_args_bytes.decode("utf-8"),
            "policy_bundle_hash": policy_bundle_hash,
            "config_identity_hash": config_identity_hash,
            "principal_identity": principal_identity,
        }
    )
    return sha256_hex(material)


def verify_action_digest(
    *,
    expected_digest: str,
    tool_id: str,
    tool_contract_hash: str,
    canonical_args_bytes: bytes,
    policy_bundle_hash: str,
    config_identity_hash: str,
    principal_identity: str,
) -> None:
    """
    Recompute the action digest and raise ApprovedActionMismatchError if it
    differs from *expected_digest*.  Call before every dispatch.
    """
    actual = compute_action_digest(
        tool_id=tool_id,
        tool_contract_hash=tool_contract_hash,
        canonical_args_bytes=canonical_args_bytes,
        policy_bundle_hash=policy_bundle_hash,
        config_identity_hash=config_identity_hash,
        principal_identity=principal_identity,
    )
    if actual != expected_digest:
        raise ApprovedActionMismatchError(
            f"Action digest mismatch: approved={expected_digest!r}, "
            f"recomputed={actual!r}"
        )


# ---------------------------------------------------------------------------
# Postcondition validator registry
# ---------------------------------------------------------------------------

#: Type alias for a postcondition validator function.
#: Signature: (kwargs, output, metadata) -> None  (or raise PostconditionFailedError)
PostconditionValidatorFn = Callable[[Any, Any, Any], None]


class PostconditionValidatorRegistry:
    """
    Server-owned registry of postcondition validator functions.

    Keyed by exact (validator_id, version) tuples.  Registration is
    immutable: once bound, a validator cannot be replaced (prevents
    post-registration substitution attacks).
    """

    def __init__(self) -> None:
        self._validators: dict[tuple[str, str], PostconditionValidatorFn] = {}

    def register(
        self,
        validator_id: str,
        version: str,
        fn: PostconditionValidatorFn,
    ) -> None:
        """
        Register a postcondition validator.

        Raises DuplicateToolRegistrationError if the (validator_id, version)
        is already registered with a *different* callable.
        Idempotent when the same callable is re-registered.
        """
        if not validator_id:
            raise InvalidSpecFieldError("validator_id must not be empty")
        if not version:
            raise InvalidSpecFieldError("version must not be empty")
        key = (validator_id, version)
        existing = self._validators.get(key)
        if existing is not None:
            if existing is not fn:
                raise DuplicateToolRegistrationError(
                    f"Postcondition validator ({validator_id!r}, {version!r}) already registered "
                    "with a different function; substitution rejected"
                )
            return  # idempotent
        self._validators[key] = fn

    def get(
        self, validator_id: str, version: str
    ) -> PostconditionValidatorFn | None:
        """Return the registered validator function or None."""
        return self._validators.get((validator_id, version))

    def validate(
        self,
        validator_id: str,
        version: str,
        kwargs: Any,
        output: Any,
        metadata: Any,
    ) -> None:
        """
        Run the registered validator.

        Raises MissingPostconditionValidatorError if no validator is registered
        for (validator_id, version).  Any exception from the validator function
        propagates to the caller, which should wrap it as PostconditionFailedError.
        """
        fn = self._validators.get((validator_id, version))
        if fn is None:
            raise MissingPostconditionValidatorError(
                f"No postcondition validator registered for "
                f"({validator_id!r}, {version!r})"
            )
        fn(kwargs, output, metadata)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

__all__ = [
    "ACTION_VERSION",
    "SCHEMA_VERSION",
    "ApprovedActionMismatchError",
    "CyclicValueError",
    "DuplicateToolRegistrationError",
    "EvidencePersistenceFailedError",
    "HandlerSubstitutionError",
    "HashMismatchError",
    "InputSchemaInvalidError",
    "InvalidSchemaError",
    "InvalidSpecFieldError",
    "MissingPostconditionValidatorError",
    "NonStringKeyError",
    "OutputSchemaInvalidError",
    "PostconditionFailedError",
    "PostconditionValidatorFn",
    "PostconditionValidatorRegistry",
    "RawCallableAuthorityError",
    "ToolAuthorityError",
    "ToolContractChangedError",
    "ToolRegistry",
    "ToolRegistryEntry",
    "ToolSpecV1",
    "UnknownToolError",
    "UnsupportedValueTypeError",
    "canonical_json",
    "canonicalize_args",
    "compute_action_digest",
    "make_registry_entry",
    "sha256_hex",
    "validate_input",
    "validate_output",
    "validate_value",
    "verify_action_digest",
]
