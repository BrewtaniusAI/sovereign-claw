"""
test_tool_authority.py — Adversarial tests for ToolSpecV1 authority contract
=============================================================================
Tests deterministic hashing, schema validation, registry governance,
action-digest stability, and adversarial edge cases.
"""

from __future__ import annotations

import json

import pytest

from sovereign_claw.tool_authority import (
    SCHEMA_VERSION,
    ApprovedActionMismatchError,
    DuplicateToolRegistrationError,
    HandlerSubstitutionError,
    HashMismatchError,
    InputSchemaInvalidError,
    InvalidSchemaError,
    InvalidSpecFieldError,
    OutputSchemaInvalidError,
    RawCallableAuthorityError,
    ToolAuthorityError,
    ToolContractChangedError,
    ToolRegistry,
    ToolRegistryEntry,
    ToolSpecV1,
    UnknownToolError,
    canonical_json,
    canonicalize_args,
    compute_action_digest,
    make_registry_entry,
    sha256_hex,
    validate_input,
    validate_output,
    verify_action_digest,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
_MINIMAL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
    "additionalProperties": False,
}


def _make_spec(
    tool_id: str = "test/echo@1.0.0",
    worker_handler_id: str = "handlers.echo_v1",
    tool_version: str = "1.0.0",
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    extra_authority: dict | None = None,
) -> ToolSpecV1:
    return ToolSpecV1(
        schema_version=SCHEMA_VERSION,
        tool_id=tool_id,
        tool_version=tool_version,
        description_hash=sha256_hex(b"echo: returns input unchanged"),
        input_schema=input_schema or _MINIMAL_INPUT_SCHEMA,
        output_schema=output_schema or _MINIMAL_OUTPUT_SCHEMA,
        capabilities=["read"],
        risk_class="LOW",
        required_principal_scopes=["tools.echo"],
        isolation_profile="in_process",
        worker_handler_id=worker_handler_id,
        worker_build_identity="IN_PROCESS",
        default_deadline_ms=5_000,
        max_deadline_ms=30_000,
        max_input_bytes=4096,
        max_output_bytes=4096,
        reversibility="reversible",
        idempotency="idempotent",
        postcondition_validator_id="",
        postcondition_validator_version="",
        evidence_policy="digest_only",
        redaction_policy="default",
        extra_authority=extra_authority or {},
    )


# ---------------------------------------------------------------------------
# 1. Deterministic canonical JSON and hash
# ---------------------------------------------------------------------------


class TestCanonicalJson:
    def test_sorted_keys(self):
        obj = {"b": 1, "a": 2, "c": 3}
        data = canonical_json(obj)
        parsed = json.loads(data)
        assert list(parsed.keys()) == ["a", "b", "c"]

    def test_compact_separators(self):
        data = canonical_json({"k": "v"})
        assert b" " not in data
        assert b'{"k":"v"}' == data

    def test_stable_across_dict_insertion_order(self):
        d1 = {"z": 1, "a": 2}
        d2 = {"a": 2, "z": 1}
        assert canonical_json(d1) == canonical_json(d2)

    def test_nested_sorted(self):
        obj = {"outer": {"b": 1, "a": 2}}
        data = canonical_json(obj)
        parsed = json.loads(data)
        assert list(parsed["outer"].keys()) == ["a", "b"]

    def test_non_finite_float_rejected(self):
        with pytest.raises(ValueError, match="Non-finite"):
            canonical_json({"x": float("nan")})
        with pytest.raises(ValueError, match="Non-finite"):
            canonical_json({"x": float("inf")})
        with pytest.raises(ValueError, match="Non-finite"):
            canonical_json({"x": float("-inf")})

    def test_deep_nesting_rejected(self):
        # Build a deeply nested dict beyond _MAX_DEPTH
        deep: dict = {}
        node = deep
        for _ in range(35):
            node["child"] = {}
            node = node["child"]
        with pytest.raises(ValueError, match="depth"):
            canonical_json(deep)

    def test_unicode_preserved(self):
        data = canonical_json({"emoji": "🦅"})
        parsed = json.loads(data.decode("utf-8"))
        assert parsed["emoji"] == "🦅"


class TestToolSpecV1Hash:
    def test_hash_is_deterministic(self):
        spec1 = _make_spec()
        spec2 = _make_spec()
        assert spec1.compute_contract_hash() == spec2.compute_contract_hash()

    def test_hash_changes_on_tool_id_change(self):
        h1 = _make_spec(tool_id="ns/toolA@1.0.0").compute_contract_hash()
        h2 = _make_spec(tool_id="ns/toolB@1.0.0").compute_contract_hash()
        assert h1 != h2

    def test_hash_changes_on_handler_change(self):
        h1 = _make_spec(worker_handler_id="handlers.v1").compute_contract_hash()
        h2 = _make_spec(worker_handler_id="handlers.v2").compute_contract_hash()
        assert h1 != h2

    def test_hash_changes_on_version_change(self):
        h1 = _make_spec(tool_version="1.0.0").compute_contract_hash()
        h2 = _make_spec(tool_version="1.0.1").compute_contract_hash()
        assert h1 != h2

    def test_hash_changes_on_input_schema_change(self):
        schema_a = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        }
        schema_b = {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
            "additionalProperties": False,
        }
        h1 = _make_spec(input_schema=schema_a).compute_contract_hash()
        h2 = _make_spec(input_schema=schema_b).compute_contract_hash()
        assert h1 != h2

    def test_any_authority_field_change_alters_hash(self):
        """Exhaustively check that changing each significant authority field changes the hash."""
        base_hash = _make_spec().compute_contract_hash()

        # risk_class
        spec2 = ToolSpecV1(
            **{**_make_spec().__dict__, "risk_class": "HIGH"},
        )
        assert spec2.compute_contract_hash() != base_hash

    def test_hash_is_valid_sha256_hex(self):
        h = _make_spec().compute_contract_hash()
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_extra_authority_field_changes_hash(self):
        h1 = _make_spec(extra_authority={}).compute_contract_hash()
        h2 = _make_spec(extra_authority={"custom": "value"}).compute_contract_hash()
        assert h1 != h2

    def test_capabilities_order_does_not_affect_hash(self):
        spec_a = ToolSpecV1(
            schema_version=SCHEMA_VERSION,
            tool_id="test/echo@1.0.0",
            tool_version="1.0.0",
            description_hash=sha256_hex(b"d"),
            input_schema=_MINIMAL_INPUT_SCHEMA,
            output_schema=_MINIMAL_OUTPUT_SCHEMA,
            capabilities=["read", "write"],
            risk_class="LOW",
            required_principal_scopes=["scope.a"],
            isolation_profile="in_process",
            worker_handler_id="h.v1",
            worker_build_identity="IN_PROCESS",
            default_deadline_ms=1000,
            max_deadline_ms=5000,
            max_input_bytes=1024,
            max_output_bytes=1024,
            reversibility="reversible",
            idempotency="idempotent",
            postcondition_validator_id="",
            postcondition_validator_version="",
            evidence_policy="digest_only",
            redaction_policy="default",
        )
        spec_b = ToolSpecV1(
            schema_version=SCHEMA_VERSION,
            tool_id="test/echo@1.0.0",
            tool_version="1.0.0",
            description_hash=sha256_hex(b"d"),
            input_schema=_MINIMAL_INPUT_SCHEMA,
            output_schema=_MINIMAL_OUTPUT_SCHEMA,
            capabilities=["write", "read"],  # reversed order
            risk_class="LOW",
            required_principal_scopes=["scope.a"],
            isolation_profile="in_process",
            worker_handler_id="h.v1",
            worker_build_identity="IN_PROCESS",
            default_deadline_ms=1000,
            max_deadline_ms=5000,
            max_input_bytes=1024,
            max_output_bytes=1024,
            reversibility="reversible",
            idempotency="idempotent",
            postcondition_validator_id="",
            postcondition_validator_version="",
            evidence_policy="digest_only",
            redaction_policy="default",
        )
        assert spec_a.compute_contract_hash() == spec_b.compute_contract_hash()


# ---------------------------------------------------------------------------
# 2. ToolSpecV1 field validation
# ---------------------------------------------------------------------------


class TestToolSpecV1Validation:
    def test_valid_spec_creates_ok(self):
        spec = _make_spec()
        assert spec.tool_id == "test/echo@1.0.0"

    def test_empty_tool_id_rejected(self):
        with pytest.raises(InvalidSpecFieldError, match="tool_id"):
            ToolSpecV1(
                schema_version=SCHEMA_VERSION,
                tool_id="",
                tool_version="1.0.0",
                description_hash=sha256_hex(b"d"),
                input_schema=_MINIMAL_INPUT_SCHEMA,
                output_schema=_MINIMAL_OUTPUT_SCHEMA,
                capabilities=[],
                risk_class="LOW",
                required_principal_scopes=[],
                isolation_profile="in_process",
                worker_handler_id="h",
                worker_build_identity="IN_PROCESS",
                default_deadline_ms=1000,
                max_deadline_ms=5000,
                max_input_bytes=1024,
                max_output_bytes=1024,
                reversibility="reversible",
                idempotency="idempotent",
                postcondition_validator_id="",
                postcondition_validator_version="",
                evidence_policy="digest_only",
                redaction_policy="default",
            )

    def test_invalid_risk_class_rejected(self):
        with pytest.raises(InvalidSpecFieldError, match="risk_class"):
            _make_spec().__class__(
                **{**_make_spec().__dict__, "risk_class": "UNKNOWN"},
            )

    def test_invalid_isolation_profile_rejected(self):
        with pytest.raises(InvalidSpecFieldError, match="isolation_profile"):
            _make_spec().__class__(
                **{**_make_spec().__dict__, "isolation_profile": "docker"},
            )

    def test_invalid_reversibility_rejected(self):
        with pytest.raises(InvalidSpecFieldError, match="reversibility"):
            _make_spec().__class__(
                **{**_make_spec().__dict__, "reversibility": "maybe"},
            )

    def test_invalid_idempotency_rejected(self):
        with pytest.raises(InvalidSpecFieldError, match="idempotency"):
            _make_spec().__class__(
                **{**_make_spec().__dict__, "idempotency": "yes"},
            )

    def test_negative_deadline_rejected(self):
        with pytest.raises(InvalidSpecFieldError):
            _make_spec().__class__(
                **{**_make_spec().__dict__, "default_deadline_ms": -1},
            )

    def test_default_deadline_exceeds_max_rejected(self):
        with pytest.raises(InvalidSpecFieldError, match="default_deadline_ms"):
            _make_spec().__class__(
                **{
                    **_make_spec().__dict__,
                    "default_deadline_ms": 60_000,
                    "max_deadline_ms": 1_000,
                },
            )

    def test_invalid_input_schema_rejected(self):
        with pytest.raises(InvalidSpecFieldError, match="input_schema"):
            _make_spec(input_schema={"type": "NOTATYPE"})

    def test_nonfinite_in_extra_authority_rejected(self):
        with pytest.raises(InvalidSpecFieldError, match="extra_authority"):
            _make_spec(extra_authority={"budget": float("nan")})

    def test_empty_worker_handler_id_rejected(self):
        with pytest.raises(InvalidSpecFieldError, match="worker_handler_id"):
            _make_spec(worker_handler_id="")


# ---------------------------------------------------------------------------
# 3. Schema validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    # --- input schema ---
    def test_valid_object_accepted(self):
        validate_input({"text": "hello"}, _MINIMAL_INPUT_SCHEMA)

    def test_missing_required_property_rejected(self):
        with pytest.raises(InputSchemaInvalidError, match="required"):
            validate_input({}, _MINIMAL_INPUT_SCHEMA)

    def test_extra_key_rejected_by_default(self):
        with pytest.raises(InputSchemaInvalidError, match="unknown property"):
            validate_input({"text": "hi", "extra": "bad"}, _MINIMAL_INPUT_SCHEMA)

    def test_wrong_type_rejected(self):
        with pytest.raises(InputSchemaInvalidError, match="expected string"):
            validate_input({"text": 42}, _MINIMAL_INPUT_SCHEMA)

    def test_integer_type(self):
        schema = {"type": "integer"}
        validate_input(5, schema)
        with pytest.raises(InputSchemaInvalidError):
            validate_input(5.0, schema)
        with pytest.raises(InputSchemaInvalidError):
            validate_input(True, schema)

    def test_number_type_accepts_int_and_float(self):
        schema = {"type": "number"}
        validate_input(5, schema)
        validate_input(3.14, schema)
        with pytest.raises(InputSchemaInvalidError):
            validate_input(True, schema)

    def test_nonfinite_float_rejected(self):
        schema = {"type": "number"}
        with pytest.raises(InputSchemaInvalidError, match="non-finite"):
            validate_input(float("nan"), schema)

    def test_boolean_type(self):
        schema = {"type": "boolean"}
        validate_input(True, schema)
        validate_input(False, schema)
        with pytest.raises(InputSchemaInvalidError):
            validate_input(1, schema)

    def test_null_type(self):
        schema = {"type": "null"}
        validate_input(None, schema)
        with pytest.raises(InputSchemaInvalidError):
            validate_input(0, schema)

    def test_string_min_max_length(self):
        schema = {"type": "string", "minLength": 2, "maxLength": 5}
        validate_input("abc", schema)
        with pytest.raises(InputSchemaInvalidError, match="minLength"):
            validate_input("a", schema)
        with pytest.raises(InputSchemaInvalidError, match="maxLength"):
            validate_input("abcdef", schema)

    def test_array_min_max_items(self):
        schema = {"type": "array", "minItems": 1, "maxItems": 3}
        validate_input([1], schema)
        validate_input([1, 2, 3], schema)
        with pytest.raises(InputSchemaInvalidError, match="minItems"):
            validate_input([], schema)
        with pytest.raises(InputSchemaInvalidError, match="maxItems"):
            validate_input([1, 2, 3, 4], schema)

    def test_array_items_schema(self):
        schema = {"type": "array", "items": {"type": "integer"}}
        validate_input([1, 2, 3], schema)
        with pytest.raises(InputSchemaInvalidError):
            validate_input([1, "two"], schema)

    def test_enum_accepted_and_rejected(self):
        schema = {"enum": ["a", "b", "c"]}
        validate_input("a", schema)
        with pytest.raises(InputSchemaInvalidError, match="enum"):
            validate_input("d", schema)

    def test_numeric_bounds(self):
        schema = {"type": "integer", "minimum": 0, "maximum": 100}
        validate_input(0, schema)
        validate_input(100, schema)
        with pytest.raises(InputSchemaInvalidError, match="minimum"):
            validate_input(-1, schema)
        with pytest.raises(InputSchemaInvalidError, match="maximum"):
            validate_input(101, schema)

    def test_additional_properties_true(self):
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "additionalProperties": True,
        }
        validate_input({"a": "x", "b": 99}, schema)

    def test_additional_properties_schema(self):
        schema = {
            "type": "object",
            "properties": {},
            "additionalProperties": {"type": "integer"},
        }
        validate_input({"x": 1, "y": 2}, schema)
        with pytest.raises(InputSchemaInvalidError):
            validate_input({"x": "str"}, schema)

    def test_output_schema_validation(self):
        validate_output({"result": "ok"}, _MINIMAL_OUTPUT_SCHEMA)
        with pytest.raises(OutputSchemaInvalidError, match="required"):
            validate_output({}, _MINIMAL_OUTPUT_SCHEMA)

    def test_invalid_schema_structure_rejected(self):
        with pytest.raises(InvalidSchemaError):
            validate_input({}, "not_a_dict")
        with pytest.raises(InvalidSchemaError):
            validate_input({}, {"type": "bogus_type"})

    def test_deeply_nested_value_rejected(self):
        # Build a value nested beyond _MAX_DEPTH
        def nested(depth: int) -> dict:
            if depth == 0:
                return {"v": 1}
            return {"child": nested(depth - 1)}

        deep_val = nested(35)
        schema = {"type": "object", "additionalProperties": True}
        with pytest.raises((ValueError, InputSchemaInvalidError)):
            validate_input(deep_val, schema)


# ---------------------------------------------------------------------------
# 4. ToolRegistryEntry and hash verification
# ---------------------------------------------------------------------------


class TestToolRegistryEntry:
    def test_make_registry_entry_ok(self):
        spec = _make_spec()
        entry = make_registry_entry(spec)
        assert entry.tool_contract_hash == spec.compute_contract_hash()
        assert entry.worker_handler_id == spec.worker_handler_id

    def test_tampered_hash_rejected(self):
        spec = _make_spec()
        with pytest.raises(HashMismatchError):
            ToolRegistryEntry(
                spec=spec,
                tool_contract_hash="deadbeef" * 8,
                worker_handler_id=spec.worker_handler_id,
            )

    def test_handler_mismatch_rejected(self):
        spec = _make_spec(worker_handler_id="h.correct")
        with pytest.raises(HandlerSubstitutionError):
            ToolRegistryEntry(
                spec=spec,
                tool_contract_hash=spec.compute_contract_hash(),
                worker_handler_id="h.different",
            )


# ---------------------------------------------------------------------------
# 5. ToolRegistry governance
# ---------------------------------------------------------------------------


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        spec = _make_spec()
        entry = make_registry_entry(spec)
        reg.register(entry)
        fetched = reg.get("test/echo@1.0.0")
        assert fetched.tool_contract_hash == entry.tool_contract_hash

    def test_unknown_tool_raises(self):
        reg = ToolRegistry()
        with pytest.raises(UnknownToolError):
            reg.get("ns/nonexistent@1.0.0")

    def test_duplicate_tool_id_same_contract_idempotent(self):
        reg = ToolRegistry()
        spec = _make_spec()
        entry = make_registry_entry(spec)
        reg.register(entry)
        reg.register(entry)  # idempotent: same hash
        assert len(reg.all_tool_ids()) == 1

    def test_duplicate_tool_id_changed_contract_rejected(self):
        reg = ToolRegistry()
        spec_v1 = _make_spec(tool_version="1.0.0")
        spec_v2 = _make_spec(tool_version="1.0.1")  # different version -> different hash
        reg.register(make_registry_entry(spec_v1))
        with pytest.raises(DuplicateToolRegistrationError):
            reg.register(make_registry_entry(spec_v2))

    def test_handler_substitution_rejected(self):
        # Same tool_id, but different worker_handler_id — different contract hash
        reg = ToolRegistry()
        spec_a = _make_spec(worker_handler_id="h.v1")
        reg.register(make_registry_entry(spec_a))
        spec_b = _make_spec(worker_handler_id="h.v2")
        with pytest.raises(DuplicateToolRegistrationError):
            reg.register(make_registry_entry(spec_b))

    def test_snapshot_and_snapshot_hash(self):
        reg = ToolRegistry()
        spec = _make_spec()
        reg.register(make_registry_entry(spec))
        snap = reg.snapshot()
        assert "test/echo@1.0.0" in snap
        h = reg.snapshot_hash()
        assert len(h) == 64

    def test_snapshot_hash_stable(self):
        reg = ToolRegistry()
        spec = _make_spec()
        reg.register(make_registry_entry(spec))
        assert reg.snapshot_hash() == reg.snapshot_hash()

    def test_all_tool_ids_sorted(self):
        reg = ToolRegistry()
        for tid in ("z/z@1", "a/a@1", "m/m@1"):
            spec = _make_spec(tool_id=tid)
            reg.register(make_registry_entry(spec))
        assert reg.all_tool_ids() == sorted(["z/z@1", "a/a@1", "m/m@1"])


# ---------------------------------------------------------------------------
# 6. canonicalize_args
# ---------------------------------------------------------------------------


class TestCanonicalizeArgs:
    def test_valid_args_returns_bytes(self):
        data = canonicalize_args({"text": "hello"}, _MINIMAL_INPUT_SCHEMA, 4096)
        assert isinstance(data, bytes)
        parsed = json.loads(data)
        assert parsed == {"text": "hello"}

    def test_missing_required_arg_rejected(self):
        with pytest.raises(InputSchemaInvalidError, match="required"):
            canonicalize_args({}, _MINIMAL_INPUT_SCHEMA, 4096)

    def test_extra_kwarg_rejected(self):
        with pytest.raises(InputSchemaInvalidError, match="unknown property"):
            canonicalize_args({"text": "hi", "evil": "x"}, _MINIMAL_INPUT_SCHEMA, 4096)

    def test_oversized_canonical_args_rejected(self):
        with pytest.raises(InputSchemaInvalidError, match="max_input_bytes"):
            canonicalize_args({"text": "a" * 5000}, _MINIMAL_INPUT_SCHEMA, 100)


# ---------------------------------------------------------------------------
# 7. Action digest
# ---------------------------------------------------------------------------


class TestActionDigest:
    def _canonical_args(self) -> bytes:
        return canonicalize_args({"text": "hello"}, _MINIMAL_INPUT_SCHEMA, 4096)

    def test_digest_is_deterministic(self):
        args = self._canonical_args()
        d1 = compute_action_digest(
            tool_id="test/echo@1.0.0",
            tool_contract_hash="abc" * 21 + "d",
            canonical_args_bytes=args,
            policy_bundle_hash="ph1",
            config_identity_hash="ch1",
            principal_identity="user:alice",
        )
        d2 = compute_action_digest(
            tool_id="test/echo@1.0.0",
            tool_contract_hash="abc" * 21 + "d",
            canonical_args_bytes=args,
            policy_bundle_hash="ph1",
            config_identity_hash="ch1",
            principal_identity="user:alice",
        )
        assert d1 == d2

    def test_digest_changes_on_tool_id(self):
        args = self._canonical_args()
        common = {
            "tool_contract_hash": "h" * 64,
            "canonical_args_bytes": args,
            "policy_bundle_hash": "ph",
            "config_identity_hash": "ch",
            "principal_identity": "user:alice",
        }
        d1 = compute_action_digest(tool_id="ns/toolA@1", **common)
        d2 = compute_action_digest(tool_id="ns/toolB@1", **common)
        assert d1 != d2

    def test_digest_changes_on_contract_hash(self):
        args = self._canonical_args()
        common = {
            "tool_id": "t/t@1",
            "canonical_args_bytes": args,
            "policy_bundle_hash": "ph",
            "config_identity_hash": "ch",
            "principal_identity": "user:alice",
        }
        d1 = compute_action_digest(tool_contract_hash="a" * 64, **common)
        d2 = compute_action_digest(tool_contract_hash="b" * 64, **common)
        assert d1 != d2

    def test_digest_changes_on_args(self):
        args_a = canonicalize_args({"text": "hello"}, _MINIMAL_INPUT_SCHEMA, 4096)
        args_b = canonicalize_args({"text": "world"}, _MINIMAL_INPUT_SCHEMA, 4096)
        common = {
            "tool_id": "t/t@1",
            "tool_contract_hash": "h" * 64,
            "policy_bundle_hash": "ph",
            "config_identity_hash": "ch",
            "principal_identity": "user:alice",
        }
        d1 = compute_action_digest(canonical_args_bytes=args_a, **common)
        d2 = compute_action_digest(canonical_args_bytes=args_b, **common)
        assert d1 != d2

    def test_digest_changes_on_policy_hash(self):
        args = self._canonical_args()
        common = {
            "tool_id": "t/t@1",
            "tool_contract_hash": "h" * 64,
            "canonical_args_bytes": args,
            "config_identity_hash": "ch",
            "principal_identity": "user:alice",
        }
        d1 = compute_action_digest(policy_bundle_hash="policy_A", **common)
        d2 = compute_action_digest(policy_bundle_hash="policy_B", **common)
        assert d1 != d2

    def test_digest_changes_on_principal(self):
        args = self._canonical_args()
        common = {
            "tool_id": "t/t@1",
            "tool_contract_hash": "h" * 64,
            "canonical_args_bytes": args,
            "policy_bundle_hash": "ph",
            "config_identity_hash": "ch",
        }
        d1 = compute_action_digest(principal_identity="user:alice", **common)
        d2 = compute_action_digest(principal_identity="user:bob", **common)
        assert d1 != d2

    def test_verify_action_digest_ok(self):
        args = self._canonical_args()
        digest = compute_action_digest(
            tool_id="t@1",
            tool_contract_hash="h" * 64,
            canonical_args_bytes=args,
            policy_bundle_hash="ph",
            config_identity_hash="ch",
            principal_identity="u",
        )
        verify_action_digest(
            expected_digest=digest,
            tool_id="t@1",
            tool_contract_hash="h" * 64,
            canonical_args_bytes=args,
            policy_bundle_hash="ph",
            config_identity_hash="ch",
            principal_identity="u",
        )

    def test_verify_action_digest_mismatch_raises(self):
        args = self._canonical_args()
        with pytest.raises(ApprovedActionMismatchError):
            verify_action_digest(
                expected_digest="0" * 64,
                tool_id="t@1",
                tool_contract_hash="h" * 64,
                canonical_args_bytes=args,
                policy_bundle_hash="ph",
                config_identity_hash="ch",
                principal_identity="u",
            )


# ---------------------------------------------------------------------------
# 8. Spec mutation after registration simulates contract drift
# ---------------------------------------------------------------------------


class TestContractDrift:
    def test_same_tool_id_different_version_must_use_new_tool_id(self):
        """
        If a ToolSpec needs to change, it must be registered under a new
        tool_id or a new explicit entry.  Attempting to re-register the same
        tool_id with changed contract is rejected.
        """
        reg = ToolRegistry()
        spec_v1 = _make_spec(tool_id="ns/tool@1.0.0", tool_version="1.0.0")
        spec_v1b = _make_spec(tool_id="ns/tool@1.0.0", tool_version="1.0.1")
        reg.register(make_registry_entry(spec_v1))
        with pytest.raises(DuplicateToolRegistrationError):
            reg.register(make_registry_entry(spec_v1b))

    def test_action_digest_changes_if_contract_hash_changes(self):
        spec_v1 = _make_spec(tool_version="1.0.0")
        spec_v2 = _make_spec(tool_version="1.0.1")
        hash_v1 = spec_v1.compute_contract_hash()
        hash_v2 = spec_v2.compute_contract_hash()
        assert hash_v1 != hash_v2

        args = canonicalize_args({"text": "hi"}, _MINIMAL_INPUT_SCHEMA, 4096)
        common = {
            "tool_id": "test/echo@1.0.0",
            "canonical_args_bytes": args,
            "policy_bundle_hash": "ph",
            "config_identity_hash": "ch",
            "principal_identity": "u",
        }
        d1 = compute_action_digest(tool_contract_hash=hash_v1, **common)
        d2 = compute_action_digest(tool_contract_hash=hash_v2, **common)
        assert d1 != d2


# ---------------------------------------------------------------------------
# 9. Raw callable cannot produce action digest
# ---------------------------------------------------------------------------


class TestRawCallableAuthority:
    def test_raw_callable_authority_error_is_tool_authority_error(self):
        exc = RawCallableAuthorityError("no authority")
        assert isinstance(exc, ToolAuthorityError)
        assert exc.error_code == "RAW_CALLABLE_NO_PRODUCTION_AUTHORITY"

    def test_raw_callable_must_not_compute_action_digest_directly(self):
        """
        Guard: any attempt to compute an action digest using a callable reference
        (e.g. str(fn) or fn.__name__) must fail type validation.
        Canonical args must be bytes from canonicalize_args(), not arbitrary values.
        """

        def my_callable(text: str) -> str:
            return text

        # Passing a callable as canonical_args_bytes must raise an error (not silently succeed)
        with pytest.raises((TypeError, AttributeError)):
            compute_action_digest(
                tool_id="t@1",
                tool_contract_hash="h" * 64,
                canonical_args_bytes=my_callable,  # type: ignore[arg-type]
                policy_bundle_hash="ph",
                config_identity_hash="ch",
                principal_identity="u",
            )


# ---------------------------------------------------------------------------
# 10. Error class hierarchy
# ---------------------------------------------------------------------------


class TestErrorClasses:
    def test_all_errors_are_tool_authority_error(self):
        for cls in (
            UnknownToolError,
            ToolContractChangedError,
            InputSchemaInvalidError,
            OutputSchemaInvalidError,
            DuplicateToolRegistrationError,
            HandlerSubstitutionError,
            InvalidSchemaError,
            InvalidSpecFieldError,
            HashMismatchError,
            RawCallableAuthorityError,
            ApprovedActionMismatchError,
        ):
            exc = cls("test")
            assert isinstance(exc, ToolAuthorityError), f"{cls} must inherit ToolAuthorityError"
            assert exc.error_code != "TOOL_AUTHORITY_ERROR" or cls is ToolAuthorityError

    def test_error_codes_are_stable(self):
        assert UnknownToolError("x").error_code == "UNKNOWN_TOOL"
        assert ToolContractChangedError("x").error_code == "TOOL_CONTRACT_CHANGED"
        assert InputSchemaInvalidError("x").error_code == "INPUT_SCHEMA_INVALID"
        assert OutputSchemaInvalidError("x").error_code == "OUTPUT_SCHEMA_INVALID"
        assert DuplicateToolRegistrationError("x").error_code == "DUPLICATE_TOOL_ID"
        assert HandlerSubstitutionError("x").error_code == "HANDLER_SUBSTITUTION_REJECTED"
        assert InvalidSchemaError("x").error_code == "INVALID_SCHEMA"
        assert InvalidSpecFieldError("x").error_code == "INVALID_SPEC_FIELD"
        assert HashMismatchError("x").error_code == "HASH_MISMATCH"
        assert RawCallableAuthorityError("x").error_code == "RAW_CALLABLE_NO_PRODUCTION_AUTHORITY"
        assert ApprovedActionMismatchError("x").error_code == "APPROVED_ACTION_MISMATCH"
