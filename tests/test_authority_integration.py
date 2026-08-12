"""
test_authority_integration.py — Adversarial tests for #40 production authority integration.

Covers:
- oneOf (exactly one) vs anyOf (at least one)
- Cycle detection in canonical_json
- Non-string mapping keys rejected
- Malformed empty/non-list combinators rejected
- Unsupported value types rejected
- Governed Orchestrator preview: authority metadata, unregistered-tool rejection
- Governed Orchestrator execute: contract drift halts with zero tool calls
- Governed Orchestrator execute: output schema violation halts
- ProofVault authority events contain hashes/metadata but not file contents
- Scoped filesystem: traversal/absolute/NUL/symlink/special-file/overwrite/byte-cap attacks
- register_all governed path
- Error code stability for new error classes
"""

from __future__ import annotations

import json as _json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

import sovereign_claw.policy_engine as policy_engine_module
from sovereign_claw.tool_authority import (
    CyclicValueError,
    EvidencePersistenceFailedError,
    InvalidSchemaError,
    NonStringKeyError,
    PostconditionFailedError,
    PostconditionValidatorRegistry,
    ToolAuthorityError,
    ToolRegistry,
    ToolRegistryEntry,
    ToolSpecV1,
    UnsupportedValueTypeError,
    canonical_json,
    make_registry_entry,
    sha256_hex,
    validate_value,
)
from sovereign_claw.tools_basic import (
    BUILTIN_POSTCONDITION_VALIDATORS,
    GOVERNED_TOOL_REGISTRY,
    TOOL_SPEC_V1_ECHO,
    TOOL_SPEC_V1_LIST_DIR,
    TOOL_SPEC_V1_READ_FILE,
    TOOL_SPEC_V1_WRITE_JSON,
    create_filesystem_capability,
    register_all,
    scoped_list_directory,
    scoped_read_text_file,
    scoped_write_json_file,
)

# ── Shared fixtures ────────────────────────────────────────────────────────────


def _make_spec(
    tool_id: str = "test.tool",
    tool_version: str = "1.0.0",
    worker_handler_id: str = "test.handler",
    risk_class: str = "LOW",
    input_schema: Any = None,
    output_schema: Any = None,
    extra_authority: dict | None = None,
) -> ToolSpecV1:
    if input_schema is None:
        input_schema = {
            "type": "object",
            "required": ["text"],
            "properties": {"text": {"type": "string"}},
            "additionalProperties": False,
        }
    if output_schema is None:
        output_schema = {"type": "string"}
    return ToolSpecV1(
        schema_version="1",
        tool_id=tool_id,
        tool_version=tool_version,
        description_hash=sha256_hex(b"test description"),
        input_schema=input_schema,
        output_schema=output_schema,
        capabilities=[],
        risk_class=risk_class,
        required_principal_scopes=[],
        isolation_profile="in_process",
        worker_handler_id=worker_handler_id,
        worker_build_identity="IN_PROCESS",
        default_deadline_ms=5_000,
        max_deadline_ms=30_000,
        max_input_bytes=64 * 1024,
        max_output_bytes=256 * 1024,
        reversibility="reversible",
        idempotency="idempotent",
        postcondition_validator_id="",
        postcondition_validator_version="",
        evidence_policy="digest_only",
        redaction_policy="default",
        extra_authority=extra_authority or {},
    )


def _make_registry_with(
    tool_id: str = "test.tool",
    worker_handler_id: str = "test.handler",
) -> tuple[ToolRegistry, ToolRegistryEntry]:
    spec = _make_spec(tool_id=tool_id, worker_handler_id=worker_handler_id)
    entry = make_registry_entry(spec)
    registry = ToolRegistry()
    registry.register(entry)
    return registry, entry


# ── Part 1: Hardened canonical_json ───────────────────────────────────────────


class TestCyclicValueDetection:
    def test_dict_self_reference_rejected(self):
        d: dict[str, Any] = {}
        d["self"] = d
        with pytest.raises(CyclicValueError, match="Cyclic"):
            canonical_json(d)

    def test_list_self_reference_rejected(self):
        lst: list[Any] = []
        lst.append(lst)
        with pytest.raises(CyclicValueError, match="Cyclic"):
            canonical_json(lst)

    def test_nested_cycle_rejected(self):
        inner: dict[str, Any] = {}
        outer: dict[str, Any] = {"inner": inner}
        inner["outer"] = outer
        with pytest.raises(CyclicValueError, match="Cyclic"):
            canonical_json(outer)

    def test_error_code_is_stable(self):
        assert CyclicValueError("x").error_code == "CYCLIC_VALUE"
        assert isinstance(CyclicValueError("x"), ToolAuthorityError)


class TestNonStringKeyRejection:
    def test_int_key_rejected(self):
        with pytest.raises(NonStringKeyError, match="Mapping keys must be str"):
            canonical_json({1: "value"})  # type: ignore[dict-item]

    def test_none_key_rejected(self):
        with pytest.raises(NonStringKeyError, match="Mapping keys must be str"):
            canonical_json({None: "value"})  # type: ignore[dict-item]

    def test_nested_int_key_rejected(self):
        with pytest.raises(NonStringKeyError):
            canonical_json({"outer": {42: "inner"}})  # type: ignore[dict-item]

    def test_error_code_is_stable(self):
        assert NonStringKeyError("x").error_code == "NON_STRING_KEY"
        assert isinstance(NonStringKeyError("x"), ToolAuthorityError)


class TestUnsupportedValueType:
    def test_set_rejected(self):
        with pytest.raises(UnsupportedValueTypeError, match="Unsupported value type"):
            canonical_json({1, 2, 3})  # type: ignore[arg-type]

    def test_tuple_rejected(self):
        # tuples are list-like but not JSON-native; canonical_json requires list
        with pytest.raises(UnsupportedValueTypeError):
            canonical_json((1, 2, 3))  # type: ignore[arg-type]

    def test_bytes_rejected(self):
        with pytest.raises(UnsupportedValueTypeError):
            canonical_json(b"hello")  # type: ignore[arg-type]

    def test_error_code_is_stable(self):
        assert UnsupportedValueTypeError("x").error_code == "UNSUPPORTED_VALUE_TYPE"
        assert isinstance(UnsupportedValueTypeError("x"), ToolAuthorityError)


# ── Part 2: oneOf vs anyOf semantics ──────────────────────────────────────────


class TestOneOfExactlyOneSchema:
    """oneOf must match exactly one sub-schema (not 'at least one' like anyOf)."""

    def _one_of_schema(self) -> dict:
        return {
            "oneOf": [
                {"type": "string"},
                {"type": "integer"},
            ]
        }

    def test_matches_exactly_one_string(self):
        validate_value("hello", self._one_of_schema())

    def test_matches_exactly_one_integer(self):
        validate_value(42, self._one_of_schema())

    def test_matches_neither_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            validate_value(3.14, self._one_of_schema())

    def test_matches_both_raises(self):
        # A boolean matches neither string nor integer (bool is not integer here)
        # but number/integer both match for a value that satisfies both
        schema = {
            "oneOf": [
                {"type": "integer"},
                {"type": "integer"},  # duplicate: both would match 5
            ]
        }
        with pytest.raises(ValueError, match="exactly one"):
            validate_value(5, schema)

    def test_any_of_allows_multiple_matches(self):
        # anyOf should succeed even if multiple match
        schema = {
            "anyOf": [
                {"type": "integer"},
                {"type": "integer"},  # duplicate
            ]
        }
        validate_value(5, schema)  # should NOT raise

    def test_malformed_one_of_empty_list_raises(self):
        with pytest.raises(InvalidSchemaError, match="oneOf.*non-empty"):
            validate_value("x", {"oneOf": []})

    def test_malformed_any_of_empty_list_raises(self):
        with pytest.raises(InvalidSchemaError, match="anyOf.*non-empty"):
            validate_value("x", {"anyOf": []})

    def test_malformed_one_of_non_list_raises(self):
        with pytest.raises(InvalidSchemaError):
            from sovereign_claw.tool_authority import _validate_schema_structure

            _validate_schema_structure({"oneOf": "not-a-list"})

    def test_malformed_any_of_non_list_raises(self):
        with pytest.raises(InvalidSchemaError):
            from sovereign_claw.tool_authority import _validate_schema_structure

            _validate_schema_structure({"anyOf": "not-a-list"})


# ── Part 3: Schema structure validation ───────────────────────────────────────


class TestSchemaStructureNesting:
    def test_deep_schema_nesting_rejected(self):
        from sovereign_claw.tool_authority import _MAX_DEPTH, _validate_schema_structure

        # Build a schema nested deeper than _MAX_DEPTH
        schema: dict = {"type": "object", "properties": {}, "additionalProperties": False}
        current = schema
        for i in range(_MAX_DEPTH + 2):
            child: dict = {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            }
            current["properties"][f"level_{i}"] = child
            current = child
        with pytest.raises(InvalidSchemaError, match="depth"):
            _validate_schema_structure(schema)

    def test_anyof_non_empty_list_validated_recursively(self):
        from sovereign_claw.tool_authority import _validate_schema_structure

        # Each sub-schema in anyOf must be structurally valid
        with pytest.raises(InvalidSchemaError):
            _validate_schema_structure({"anyOf": [{"type": "invalid_type"}]})


# ── Part 4: Error code stability for new classes ──────────────────────────────


class TestNewErrorCodeStability:
    def test_cyclic_value_error_code(self):
        assert CyclicValueError("x").error_code == "CYCLIC_VALUE"

    def test_non_string_key_error_code(self):
        assert NonStringKeyError("x").error_code == "NON_STRING_KEY"

    def test_unsupported_value_type_error_code(self):
        assert UnsupportedValueTypeError("x").error_code == "UNSUPPORTED_VALUE_TYPE"

    def test_all_new_errors_inherit_tool_authority_error(self):
        for cls in (CyclicValueError, NonStringKeyError, UnsupportedValueTypeError):
            assert isinstance(cls("x"), ToolAuthorityError)


# ── Part 5: Governed Orchestrator preview ─────────────────────────────────────


class _EchoBackend:
    """Stub LLM backend that always proposes echo_text."""

    def __init__(self, tool: str = "echo_text", kwargs: dict | None = None):
        self._tool = tool
        self._kwargs = kwargs or {"text": "hello"}

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        return {
            "tool": self._tool,
            "kwargs": self._kwargs,
            "comment": "test",
        }


def _make_governed_orchestrator(
    tool_id: str = "builtin.echo_text",
    echo_fn: Any = None,
):
    from sovereign_claw.orchestrator import Orchestrator

    spec = TOOL_SPEC_V1_ECHO
    entry = make_registry_entry(spec)
    registry = ToolRegistry()
    registry.register(entry)

    echo = echo_fn if echo_fn is not None else (lambda text: text)

    orch = Orchestrator(
        # In governed mode LLM proposes tool_id as the action name
        llm_backend=_EchoBackend(tool=tool_id, kwargs={"text": "hello"}),
        tool_registry=registry,
    )
    # Register callable via the governed handler binding keyed by worker_handler_id.
    orch.register_governed_handler(entry.worker_handler_id, echo)
    return orch, registry, entry


class TestGovernedOrchestratorPreview:
    def _manifold(self) -> Any:
        from sovereign_claw.thermodynamics import TaskManifold

        return TaskManifold(objective="test", t_max_steps=3)

    def test_governed_preview_returns_authority_metadata(self):
        orch, _registry, entry = _make_governed_orchestrator()
        result = orch.preview(self._manifold())
        assert result["governed"] is True
        assert "authority_metadata" in result
        meta = result["authority_metadata"]
        assert meta["tool_id"] == "builtin.echo_text"
        assert meta["tool_contract_hash"] == entry.tool_contract_hash
        assert "registry_snapshot_hash" in meta
        assert "worker_handler_id" in meta
        assert "isolation_profile" in meta
        assert "risk_class" in meta

    def test_governed_preview_action_digest_present(self):
        orch, _registry, _entry = _make_governed_orchestrator()
        result = orch.preview(self._manifold())
        assert result["action_digest"] is not None
        assert len(result["action_digest"]) == 64

    def test_governed_preview_unregistered_tool_not_approvable(self):
        from sovereign_claw.orchestrator import Orchestrator

        # Empty registry, tool proposed as a raw callable (not registered in registry)
        registry = ToolRegistry()
        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="unregistered_tool", kwargs={"text": "hi"}),
            tool_registry=registry,
        )
        result = orch.preview(self._manifold())
        assert result["approvable"] is False
        assert result["status"] == "preview-unknown-tool"

    def test_governed_preview_action_digest_stable_across_calls(self):
        orch, _registry, _entry = _make_governed_orchestrator()
        r1 = orch.preview(self._manifold())
        r2 = orch.preview(self._manifold())
        assert r1["action_digest"] == r2["action_digest"]

    def test_ungoverned_preview_no_authority_metadata(self):
        from sovereign_claw.orchestrator import Orchestrator

        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="echo_text", kwargs={"text": "hi"}),
        )
        orch.register_tool("echo_text", lambda text: text)
        result = orch.preview(self._manifold())
        assert result["governed"] is False
        assert "authority_metadata" not in result

    def test_governed_preview_input_schema_violation_not_approvable(self):
        """Extra kwargs rejected in governed mode before approval."""
        from sovereign_claw.orchestrator import Orchestrator

        spec = TOOL_SPEC_V1_ECHO  # input schema only allows "text"
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        orch = Orchestrator(
            llm_backend=_EchoBackend(
                tool="builtin.echo_text",
                kwargs={"text": "hi", "extra_kwarg": "should_fail"},
            ),
            tool_registry=registry,
        )
        result = orch.preview(self._manifold())
        assert result["approvable"] is False

    def test_governed_action_digest_changes_with_args(self):
        from sovereign_claw.orchestrator import Orchestrator

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        orch1 = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "foo"}),
            tool_registry=registry,
        )

        orch2 = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "bar"}),
            tool_registry=registry,
        )

        manifold = self._manifold()
        r1 = orch1.preview(manifold)
        r2 = orch2.preview(manifold)
        assert r1["action_digest"] != r2["action_digest"]


# ── Part 6: Governed Orchestrator execute ─────────────────────────────────────


class _OnceEchoBackend:
    """LLM backend that proposes a tool on first call then HALT."""

    def __init__(self, tool: str, kwargs: dict):
        self._tool = tool
        self._kwargs = kwargs
        self._called = 0

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        self._called += 1
        if self._called == 1:
            return {"tool": self._tool, "kwargs": self._kwargs, "comment": ""}
        return {"tool": "HALT", "kwargs": {}, "comment": "done"}


def _manifold_with_digest(approved_digest: str) -> Any:
    from sovereign_claw.thermodynamics import TaskManifold

    return TaskManifold(
        objective="test",
        t_max_steps=5,
        metadata={"approved_action_digest": approved_digest},
    )


class TestGovernedOrchestratorExecute:
    def _manifold(self, approved: str | None = None) -> Any:
        from sovereign_claw.thermodynamics import TaskManifold

        meta: dict = {}
        if approved:
            meta["approved_action_digest"] = approved
        return TaskManifold(objective="test", t_max_steps=5, metadata=meta)

    def _governed_orch_for_echo(self, echo_fn: Any = None) -> Any:
        from sovereign_claw.orchestrator import Orchestrator

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)
        fn = echo_fn if echo_fn is not None else (lambda text: text)
        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
        )
        orch.register_governed_handler(entry.worker_handler_id, fn)
        return orch, registry, entry

    def test_governed_execute_succeeds_with_correct_digest(self):
        """With the correct approved digest the action executes (not mismatch halted)."""
        orch, _registry, _entry = self._governed_orch_for_echo()

        preview_manifold = self._manifold()
        preview = orch.preview(preview_manifold)
        assert preview["approvable"] is True
        digest = preview["action_digest"]

        exec_manifold = self._manifold(approved=digest)
        receipt = orch.execute(exec_manifold)
        # The one approved step runs, then APPROVAL_SCOPE_EXHAUSTED terminates
        # the loop — NOT APPROVED_ACTION_MISMATCH.
        assert receipt.halt_reason != "APPROVED_ACTION_MISMATCH"
        assert receipt.steps >= 1  # at least one step executed

    def test_governed_execute_wrong_digest_zero_tool_calls(self):
        call_count = {"n": 0}

        def echo_fn(text):
            call_count["n"] += 1
            return text

        orch, _registry, _entry = self._governed_orch_for_echo(echo_fn=echo_fn)

        exec_manifold = self._manifold(approved="a" * 64)  # wrong digest
        receipt = orch.execute(exec_manifold)
        assert call_count["n"] == 0  # zero tool calls
        assert receipt.halt_reason == "APPROVED_ACTION_MISMATCH"

    def test_governed_execute_output_schema_violation_halts(self):
        from sovereign_claw.orchestrator import Orchestrator

        # Tool that returns wrong type (int instead of string)
        spec = _make_spec(
            tool_id="test.returns_wrong_type",
            worker_handler_id="test.handler",
            output_schema={"type": "string"},
        )
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        llm = _EchoBackend(tool="test.returns_wrong_type", kwargs={"text": "hello"})
        orch = Orchestrator(llm_backend=llm, tool_registry=registry)
        orch.register_governed_handler("test.handler", lambda text: 42)  # returns int!

        exec_manifold = self._manifold()  # no approved digest -> free execute
        receipt = orch.execute(exec_manifold)
        assert "OUTPUT_SCHEMA_INVALID" in receipt.halt_reason

    def test_governed_execute_output_schema_ok_does_not_fail_with_schema_error(self):
        """Output schema validation passing must not produce OUTPUT_SCHEMA_INVALID halt."""
        orch, _registry, _entry = self._governed_orch_for_echo()
        receipt = orch.execute(self._manifold())
        # May end with T_MAX or drift-0; must NOT be output schema invalid
        assert "OUTPUT_SCHEMA_INVALID" not in (receipt.halt_reason or "")

    def test_governed_execute_logs_authority_event(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.proof_vault import ProofVault

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        vault = ProofVault()
        llm = _EchoBackend(tool="builtin.echo_text", kwargs={"text": "hello"})
        orch = Orchestrator(llm_backend=llm, tool_registry=registry, vault=vault)
        orch.register_governed_handler("builtin.echo_text.in_process", lambda text: text)

        receipt = orch.execute(self._manifold())
        # Find authority events
        events = vault.get_evidence_records(receipt.trace_id)
        authority_events = [e for e in events if e.evidence_type == "authority.tool.execution"]
        assert len(authority_events) >= 1
        # Authority event must contain tool_id and hashes, NOT raw file contents
        for ev in authority_events:
            payload = _json.loads(ev.canonical_payload)
            assert "tool_id" in payload
            assert "tool_contract_hash" in payload
            assert "action_digest" in payload
            # Must not contain file body
            assert "file_content" not in payload
            assert "file_body" not in payload

    def test_governed_execute_input_schema_violation_zero_tool_calls(self):
        from sovereign_claw.orchestrator import Orchestrator

        spec = TOOL_SPEC_V1_ECHO  # only allows "text"
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        call_count = {"n": 0}

        def echo_fn(text, extra=None):
            call_count["n"] += 1
            return text

        # Propose extra kwarg that violates input schema
        llm = _EchoBackend(
            tool="builtin.echo_text",
            kwargs={"text": "hi", "extra": "bad"},
        )
        orch = Orchestrator(llm_backend=llm, tool_registry=registry)
        orch.register_governed_handler("builtin.echo_text.in_process", echo_fn)

        receipt = orch.execute(self._manifold())
        assert call_count["n"] == 0
        assert receipt.halt_reason == "INPUT_SCHEMA_INVALID"

    def test_different_spec_same_tool_id_different_digest(self):
        """Same Python signature but different ToolSpec => different authority."""
        spec_a = _make_spec(tool_id="test.tool", worker_handler_id="handler.a")
        spec_b = ToolSpecV1(
            schema_version="1",
            tool_id="test.tool_b",  # different ID
            tool_version="1.0.0",
            description_hash=sha256_hex(b"different description"),
            input_schema={
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
                "additionalProperties": False,
            },
            output_schema={"type": "string"},
            capabilities=[],
            risk_class="MEDIUM",  # different risk class
            required_principal_scopes=[],
            isolation_profile="in_process",
            worker_handler_id="handler.b",
            worker_build_identity="IN_PROCESS",
            default_deadline_ms=5_000,
            max_deadline_ms=30_000,
            max_input_bytes=64 * 1024,
            max_output_bytes=256 * 1024,
            reversibility="reversible",
            idempotency="idempotent",
            postcondition_validator_id="",
            postcondition_validator_version="",
            evidence_policy="digest_only",
            redaction_policy="default",
        )
        assert spec_a.compute_contract_hash() != spec_b.compute_contract_hash()


# ── Part 7: Scoped filesystem capabilities ────────────────────────────────────


class TestScopedFilesystemCapability:
    def test_read_within_root_succeeds(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        (tmp_path / "hello.txt").write_text("world", encoding="utf-8")
        result = scoped_read_text_file(root_id, "hello.txt")
        assert result == "world"

    def test_absolute_path_rejected(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        with pytest.raises(ValueError, match="absolute"):
            scoped_read_text_file(root_id, "/etc/passwd")

    def test_dotdot_path_rejected(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        with pytest.raises(ValueError, match="\\.\\."):
            scoped_read_text_file(root_id, "../secret.txt")

    def test_dotdot_in_nested_path_rejected(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        with pytest.raises(ValueError, match="\\.\\."):
            scoped_read_text_file(root_id, "subdir/../../secret.txt")

    def test_nul_byte_in_path_rejected(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        with pytest.raises(ValueError, match="NUL"):
            scoped_read_text_file(root_id, "file\x00.txt")

    def test_unknown_root_id_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown filesystem capability"):
            scoped_read_text_file("fscap-nonexistent", "file.txt")

    def test_read_missing_file_raises_file_not_found(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        with pytest.raises(FileNotFoundError):
            scoped_read_text_file(root_id, "missing.txt")

    def test_read_byte_cap_enforced(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path, max_read_bytes=5)
        (tmp_path / "big.txt").write_text("hello world", encoding="utf-8")
        with pytest.raises(ValueError, match="read cap"):
            scoped_read_text_file(root_id, "big.txt")

    def test_write_json_succeeds(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path, allow_overwrite=True)
        result = scoped_write_json_file(root_id, "output.json", {"key": "value"})
        assert result == "output.json"
        assert (tmp_path / "output.json").exists()
        data = _json.loads((tmp_path / "output.json").read_bytes())
        assert data == {"key": "value"}

    def test_write_json_overwrite_denied_by_default(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path, allow_overwrite=False)
        (tmp_path / "existing.json").write_text("{}", encoding="utf-8")
        with pytest.raises(FileExistsError, match="overwrite is denied"):
            scoped_write_json_file(root_id, "existing.json", {"new": "data"})

    def test_write_json_overwrite_allowed_when_flag_set(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path, allow_overwrite=False)
        (tmp_path / "existing.json").write_text("{}", encoding="utf-8")
        result = scoped_write_json_file(root_id, "existing.json", {"new": "data"}, overwrite=True)
        assert result == "existing.json"

    def test_write_json_byte_cap_enforced(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path, max_write_bytes=5)
        with pytest.raises(ValueError, match="write cap"):
            scoped_write_json_file(root_id, "big.json", {"large": "data" * 100})

    def test_write_json_rejects_non_finite(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        with pytest.raises((ValueError, Exception)):
            scoped_write_json_file(root_id, "nan.json", float("nan"))

    def test_write_json_rejects_cycles(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        d: dict[str, Any] = {}
        d["self"] = d
        with pytest.raises((CyclicValueError, Exception)):
            scoped_write_json_file(root_id, "cycle.json", d)

    def test_write_json_absolute_path_rejected(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        with pytest.raises(ValueError, match="absolute"):
            scoped_write_json_file(root_id, "/tmp/escape.json", {"x": 1})

    def test_write_json_dotdot_rejected(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        with pytest.raises(ValueError, match="\\.\\."):
            scoped_write_json_file(root_id, "../escape.json", {"x": 1})

    def test_list_directory_within_root(self, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        root_id = create_filesystem_capability(tmp_path)
        entries = scoped_list_directory(root_id, ".")
        assert "a.txt" in entries
        assert "b.txt" in entries

    def test_list_directory_absolute_rejected(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        with pytest.raises(ValueError, match="absolute"):
            scoped_list_directory(root_id, "/etc")

    def test_list_directory_dotdot_rejected(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        with pytest.raises(ValueError, match="\\.\\."):
            scoped_list_directory(root_id, "../")

    def test_list_directory_entry_cap_enforced(self, tmp_path):
        for i in range(10):
            (tmp_path / f"file_{i}.txt").write_text(str(i))
        root_id = create_filesystem_capability(tmp_path, max_list_entries=5)
        with pytest.raises(ValueError, match="entries"):
            scoped_list_directory(root_id, ".")

    @pytest.mark.skipif(os.name == "nt", reason="symlinks may require privileges on Windows")
    def test_symlink_escape_rejected(self, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("secret", encoding="utf-8")
        root = tmp_path / "root"
        root.mkdir()
        link = root / "link.txt"
        link.symlink_to(secret)
        # The symlink target is outside the root
        root_id = create_filesystem_capability(root)
        # Reading via symlink: the symlink target is outside root
        # _safe_resolve uses .resolve() which follows symlinks
        with pytest.raises(ValueError, match="escapes"):
            scoped_read_text_file(root_id, "link.txt")

    def test_special_file_rejected(self, tmp_path):
        """Reject device/special files (use /dev/null as a known special file)."""
        if not Path("/dev/null").exists():
            pytest.skip("No /dev/null available")
        # /dev/null is a special file; create a root containing a symlink to it
        root = tmp_path / "root"
        root.mkdir()
        link = root / "devnull"
        link.symlink_to("/dev/null")
        root_id = create_filesystem_capability(root)
        with pytest.raises((ValueError, OSError)):
            scoped_read_text_file(root_id, "devnull")

    def test_write_json_digest_postcondition(self, tmp_path):
        """Verify the postcondition check: written bytes match expected digest."""
        root_id = create_filesystem_capability(tmp_path, allow_overwrite=True)
        data = {"verified": True}
        scoped_write_json_file(root_id, "verified.json", data)
        # File should contain finite canonical JSON
        written = (tmp_path / "verified.json").read_bytes()
        from sovereign_claw.tool_authority import canonical_json as cjson

        expected = cjson(data)
        assert written == expected


# ── Part 8: register_all governed path ────────────────────────────────────────


class TestRegisterAllGovernedPath:
    def test_register_all_populates_tool_registry(self):
        from sovereign_claw.orchestrator import Orchestrator

        registry = ToolRegistry()

        class _DummyLLM:
            def decide_next_action(self, *args, **kwargs):
                return {"tool": "HALT", "kwargs": {}, "comment": ""}

        orch = Orchestrator(llm_backend=_DummyLLM(), tool_registry=registry)
        register_all(orch)
        # All governed specs should be registered
        for tool_id in GOVERNED_TOOL_REGISTRY:
            entry = registry.get(tool_id)
            assert entry is not None

    def test_register_all_without_tool_registry_legacy_only(self):
        class _DummyOrch:
            def __init__(self):
                self.registered = []

            def register_tool(self, name, fn):
                self.registered.append(name)

        orch = _DummyOrch()
        register_all(orch)
        assert len(orch.registered) == 4  # echo_text, read_text_file, write_json_file, list_dir

    def test_register_all_idempotent_for_governed(self):
        from sovereign_claw.orchestrator import Orchestrator

        class _DummyLLM:
            def decide_next_action(self, *args, **kwargs):
                return {"tool": "HALT", "kwargs": {}, "comment": ""}

        registry = ToolRegistry()
        orch = Orchestrator(llm_backend=_DummyLLM(), tool_registry=registry)
        register_all(orch)
        register_all(orch)  # idempotent re-registration should not raise


# ── Part 9: ToolSpecV1 records for all built-ins ──────────────────────────────


class TestBuiltinToolSpecV1Records:
    def test_echo_spec_v1_is_valid(self):
        assert TOOL_SPEC_V1_ECHO.tool_id == "builtin.echo_text"
        h = TOOL_SPEC_V1_ECHO.compute_contract_hash()
        assert len(h) == 64  # SHA-256 hex

    def test_read_file_spec_v1_is_valid(self):
        assert TOOL_SPEC_V1_READ_FILE.tool_id == "builtin.read_text_file"
        h = TOOL_SPEC_V1_READ_FILE.compute_contract_hash()
        assert len(h) == 64

    def test_write_json_spec_v1_is_valid(self):
        assert TOOL_SPEC_V1_WRITE_JSON.tool_id == "builtin.write_json_file"
        h = TOOL_SPEC_V1_WRITE_JSON.compute_contract_hash()
        assert len(h) == 64

    def test_list_dir_spec_v1_is_valid(self):
        assert TOOL_SPEC_V1_LIST_DIR.tool_id == "builtin.list_directory"
        h = TOOL_SPEC_V1_LIST_DIR.compute_contract_hash()
        assert len(h) == 64

    def test_contract_hashes_are_stable_across_calls(self):
        """Hash must be deterministic: same spec => same hash."""
        h1 = TOOL_SPEC_V1_ECHO.compute_contract_hash()
        h2 = TOOL_SPEC_V1_ECHO.compute_contract_hash()
        assert h1 == h2

    def test_any_authority_field_change_alters_hash(self):
        """Changing any authority field must produce a different contract hash."""
        base_hash = TOOL_SPEC_V1_ECHO.compute_contract_hash()
        # Change risk_class
        modified = ToolSpecV1(
            schema_version=TOOL_SPEC_V1_ECHO.schema_version,
            tool_id=TOOL_SPEC_V1_ECHO.tool_id,
            tool_version=TOOL_SPEC_V1_ECHO.tool_version,
            description_hash=TOOL_SPEC_V1_ECHO.description_hash,
            input_schema=TOOL_SPEC_V1_ECHO.input_schema,
            output_schema=TOOL_SPEC_V1_ECHO.output_schema,
            capabilities=TOOL_SPEC_V1_ECHO.capabilities,
            risk_class="HIGH",  # changed
            required_principal_scopes=TOOL_SPEC_V1_ECHO.required_principal_scopes,
            isolation_profile=TOOL_SPEC_V1_ECHO.isolation_profile,
            worker_handler_id=TOOL_SPEC_V1_ECHO.worker_handler_id,
            worker_build_identity=TOOL_SPEC_V1_ECHO.worker_build_identity,
            default_deadline_ms=TOOL_SPEC_V1_ECHO.default_deadline_ms,
            max_deadline_ms=TOOL_SPEC_V1_ECHO.max_deadline_ms,
            max_input_bytes=TOOL_SPEC_V1_ECHO.max_input_bytes,
            max_output_bytes=TOOL_SPEC_V1_ECHO.max_output_bytes,
            reversibility=TOOL_SPEC_V1_ECHO.reversibility,
            idempotency=TOOL_SPEC_V1_ECHO.idempotency,
            postcondition_validator_id=TOOL_SPEC_V1_ECHO.postcondition_validator_id,
            postcondition_validator_version=TOOL_SPEC_V1_ECHO.postcondition_validator_version,
            evidence_policy=TOOL_SPEC_V1_ECHO.evidence_policy,
            redaction_policy=TOOL_SPEC_V1_ECHO.redaction_policy,
        )
        assert modified.compute_contract_hash() != base_hash


# ── Part 10: ProofVault authority events content ──────────────────────────────


class TestProofVaultAuthorityEventContent:
    """Authority events must contain hashes/metadata but not file contents."""

    def test_authority_event_has_no_file_content(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.proof_vault import ProofVault
        from sovereign_claw.thermodynamics import TaskManifold

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        vault = ProofVault()
        llm = _EchoBackend(tool="builtin.echo_text", kwargs={"text": "my secret text"})
        orch = Orchestrator(llm_backend=llm, tool_registry=registry, vault=vault)
        orch.register_governed_handler("builtin.echo_text.in_process", lambda text: text)

        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=5))

        events = vault.get_evidence_records(receipt.trace_id)
        authority_events = [e for e in events if e.evidence_type == "authority.tool.execution"]
        assert len(authority_events) >= 1

        for ev in authority_events:
            payload = _json.loads(ev.canonical_payload)
            # Must have hashes
            assert "tool_contract_hash" in payload
            assert "action_digest" in payload
            # Must NOT store raw text bodies
            raw_payload_str = ev.canonical_payload
            assert "my secret text" not in raw_payload_str


# ── Part 11: Fix 1 — Governed handler substitution prevention ─────────────────


class _EchoBackendScopeTest:
    """LLM backend stub for scope-related tests."""

    def __init__(self, tool: str, kwargs: dict):
        self._tool = tool
        self._kwargs = kwargs
        self._called = 0

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        self._called += 1
        if self._called == 1:
            return {"tool": self._tool, "kwargs": self._kwargs, "comment": ""}
        return {"tool": "HALT", "kwargs": {}, "comment": "done"}


class TestGovernedHandlerSubstitutionPrevented:
    """Fix 1: Governed dispatch must not call self.tools[tool_id]."""

    def test_tools_mutation_after_approval_cannot_actuate(self):
        """Mutating self.tools[tool_id] after preview must not run the substituted callable."""
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        call_log = {"original": 0, "substituted": 0}

        def original_fn(text: str) -> str:
            call_log["original"] += 1
            return text

        def substituted_fn(text: str) -> str:
            call_log["substituted"] += 1
            return text

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
        )
        orch.register_governed_handler("builtin.echo_text.in_process", original_fn)

        # Get the approved digest via preview
        manifold = TaskManifold(objective="test", t_max_steps=5)
        preview = orch.preview(manifold)
        assert preview["approvable"] is True
        digest = preview["action_digest"]

        # Attack: mutate self.tools after preview — this is the substitution vector
        orch.tools["builtin.echo_text"] = substituted_fn

        # Execute with the previously approved digest
        exec_manifold = TaskManifold(
            objective="test",
            t_max_steps=5,
            metadata={"approved_action_digest": digest},
        )
        receipt = orch.execute(exec_manifold)
        assert receipt.halt_reason != "APPROVED_ACTION_MISMATCH"
        assert call_log["substituted"] == 0, "substituted callable must never run"
        assert call_log["original"] >= 1, "original governed callable must have run"

    def test_register_governed_handler_substitution_rejected(self):
        """register_governed_handler must reject binding a different callable."""
        from sovereign_claw.orchestrator import Orchestrator

        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hi"}),
        )

        def fn_a(text):
            return text

        def fn_b(text):
            return text + "x"

        orch.register_governed_handler("builtin.echo_text.in_process", fn_a)
        with pytest.raises(ValueError, match="substitution rejected"):
            orch.register_governed_handler("builtin.echo_text.in_process", fn_b)

    def test_register_governed_handler_idempotent_same_fn(self):
        """Re-registering the same callable must not raise."""
        from sovereign_claw.orchestrator import Orchestrator

        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hi"}),
        )

        def fn(text):
            return text

        orch.register_governed_handler("builtin.echo_text.in_process", fn)
        orch.register_governed_handler("builtin.echo_text.in_process", fn)  # must not raise


# ── Part 12: Fix 2 — Principal scopes enforcement ────────────────────────────


class TestPrincipalScopesEnforcement:
    """Fix 2: Required principal scopes must be enforced at preview and dispatch."""

    def test_missing_scope_at_preview_not_approvable(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        # TOOL_SPEC_V1_READ_FILE requires "filesystem.read" scope
        spec = TOOL_SPEC_V1_READ_FILE
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        orch = Orchestrator(
            llm_backend=_EchoBackend(
                tool="builtin.read_text_file",
                kwargs={"root_id": "fscap-x", "relative_path": "f.txt"},
            ),
            tool_registry=registry,
        )

        # No scopes in manifold metadata — must be non-approvable
        manifold = TaskManifold(objective="test", t_max_steps=3)
        result = orch.preview(manifold)
        assert result["approvable"] is False
        reasons_str = str(result.get("policy_reasons", "")).lower()
        assert "scope" in reasons_str or result["status"] == "preview-missing-scopes"

    def test_scope_drift_changes_action_digest(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        spec = TOOL_SPEC_V1_ECHO  # no required scopes
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hi"}),
            tool_registry=registry,
        )

        manifold = TaskManifold(objective="test", t_max_steps=3)
        ctx_no_scope = orch.build_authenticated_principal_context(
            principal_identity="user:a", principal_scopes=[]
        )
        ctx_with_scope = orch.build_authenticated_principal_context(
            principal_identity="user:a", principal_scopes=["scope.a"]
        )
        r1 = orch.preview(manifold, principal_context=ctx_no_scope)
        r2 = orch.preview(manifold, principal_context=ctx_with_scope)
        assert r1["action_digest"] != r2["action_digest"]

    def test_missing_scope_at_execute_zero_calls(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        spec = TOOL_SPEC_V1_READ_FILE  # requires "filesystem.read"
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        call_count = {"n": 0}

        def fake_read(root_id: str, relative_path: str) -> str:
            call_count["n"] += 1
            return "content"

        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(
                tool="builtin.read_text_file",
                kwargs={"root_id": "cap-x", "relative_path": "f.txt"},
            ),
            tool_registry=registry,
        )
        orch.register_governed_handler("builtin.read_text_file.in_process", fake_read)

        # Execute without providing the required scope
        manifold = TaskManifold(objective="test", t_max_steps=5)
        receipt = orch.execute(manifold)
        assert call_count["n"] == 0, "zero tool calls"
        assert receipt.halt_reason == "MISSING_PRINCIPAL_SCOPES"

    def test_scope_drift_between_preview_and_execute_invalidates_digest(self):
        """Scope drift between preview and execute must cause APPROVED_ACTION_MISMATCH."""
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        call_count = {"n": 0}

        def echo_fn(text: str) -> str:
            call_count["n"] += 1
            return text

        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
        )
        orch.register_governed_handler("builtin.echo_text.in_process", echo_fn)

        # Preview WITH authenticated scope.
        ctx_with_scope = orch.build_authenticated_principal_context(
            principal_identity="user:a", principal_scopes=["scope.x"]
        )
        preview = orch.preview(
            TaskManifold(objective="test", t_max_steps=5), principal_context=ctx_with_scope
        )
        assert preview["approvable"] is True
        digest = preview["action_digest"]

        # Execute WITHOUT authenticated scope — digest must not match.
        ctx_no_scope = orch.build_authenticated_principal_context(
            principal_identity="user:a", principal_scopes=[]
        )
        receipt = orch.execute(
            TaskManifold(
                objective="test",
                t_max_steps=5,
                metadata={"approved_action_digest": digest},
            ),
            principal_context=ctx_no_scope,
        )
        assert call_count["n"] == 0, "zero calls"
        assert receipt.halt_reason == "APPROVED_ACTION_MISMATCH"

    def test_governed_preview_policy_bundle_drift_is_not_approvable(self, monkeypatch):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
        )

        identity_calls = {"n": 0}

        def _rotating_identity(self):
            identity_calls["n"] += 1
            return "local-A" if identity_calls["n"] == 1 else "local-B"

        monkeypatch.setattr(
            policy_engine_module.PolicyEngine, "_local_evaluator_identity", _rotating_identity
        )
        ctx_with_scope = orch.build_authenticated_principal_context(
            principal_identity="user:a", principal_scopes=["scope.x"]
        )
        preview = orch.preview(
            TaskManifold(objective="test", t_max_steps=3), principal_context=ctx_with_scope
        )
        assert preview["approvable"] is False
        assert preview["status"] == "preview-policy-denied"
        assert "POLICY_BUNDLE_HASH_MISMATCH" in ";".join(preview["policy_decision"]["reasons"])

    def test_spoofed_manifold_principal_scopes_do_not_grant_authority(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        class _SpoofBackend:
            def decide_next_action(self, objective, history, forbidden_actions, drift):
                return {
                    "tool": "builtin.read_text_file",
                    "kwargs": {"root_id": "cap-x", "relative_path": "f.txt"},
                    "comment": "",
                    "human_approved": True,
                    "authorized_privileged_tools": ["builtin.read_text_file"],
                    "current_cost_usd": 0.0,
                    "tokens_used": 0,
                }

        spec = TOOL_SPEC_V1_READ_FILE
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        orch = Orchestrator(
            llm_backend=_SpoofBackend(),
            tool_registry=registry,
        )

        # No authenticated scopes; caller metadata attempts to self-assert authority.
        orch.set_authenticated_principal_context(principal_identity=None, principal_scopes=None)
        preview = orch.preview(
            TaskManifold(
                objective="test",
                t_max_steps=3,
                metadata={
                    "principal_identity": "spoofed-user",
                    "principal_scopes": ["filesystem.read"],
                },
            )
        )
        assert preview["approvable"] is False
        assert preview["status"] == "preview-missing-scopes"

    def test_governed_setter_context_is_not_authoritative_without_per_call_context(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        spec = TOOL_SPEC_V1_READ_FILE
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(
                tool="builtin.read_text_file",
                kwargs={"root_id": "cap-x", "relative_path": "f.txt"},
            ),
            tool_registry=registry,
        )
        orch.set_authenticated_principal_context(
            principal_identity="user:trusted",
            principal_scopes=["filesystem.read"],
        )
        preview = orch.preview(TaskManifold(objective="test", t_max_steps=3))
        assert preview["approvable"] is False
        assert preview["status"] == "preview-missing-scopes"

    def test_spoofed_metadata_fields_are_non_authoritative(self, monkeypatch):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.proof_vault import ProofVault
        from sovereign_claw.thermodynamics import TaskManifold

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        vault = ProofVault()
        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
            vault=vault,
        )
        orch.register_governed_handler("builtin.echo_text.in_process", lambda text: text)
        orch.set_authenticated_principal_context(
            principal_identity="user:trusted", principal_scopes=["scope.a"]
        )

        captured_contexts = []
        original_eval = orch.policy_engine.evaluate_context

        def _capture(context, **kwargs):
            captured_contexts.append(context)
            return original_eval(context, **kwargs)

        monkeypatch.setattr(orch.policy_engine, "evaluate_context", _capture)
        receipt = orch.execute(
            TaskManifold(
                objective="test",
                t_max_steps=2,
                metadata={
                    "session_id": "spoofed-session",
                    "lane": "spoofed-lane",
                    "execution_intent_id": "spoofed-intent",
                    "approval_correlation_id": "spoofed-approval",
                    "execution_deadline_ms": 999999,
                },
            )
        )
        assert receipt.trace_id
        assert captured_contexts, "policy context must be captured"
        ctx = captured_contexts[0]
        assert ctx.session_id != "spoofed-session"
        assert ctx.lane != "spoofed-lane"
        assert ctx.execution_intent_id == "unset"
        assert ctx.approval_correlation_id == "unset"
        assert ctx.model_claims["caller_session_id"] == "spoofed-session"
        assert ctx.model_claims["caller_lane"] == "spoofed-lane"
        assert ctx.model_claims["caller_execution_intent_id"] == "spoofed-intent"
        assert ctx.model_claims["caller_approval_correlation_id"] == "spoofed-approval"
        assert ctx.model_claims["caller_execution_deadline_ms"] == 999999
        assert ctx.remaining_deadline_ms != 999999
        assert "risk_threshold" not in ctx.budget_state
        assert ctx.model_claims["caller_risk_threshold"] == 0.9

        evidence = vault.get_evidence_records(receipt.trace_id)
        policy_events = [e for e in evidence if e.evidence_type == "authority.policy.decision"]
        assert policy_events, "governed execute must write policy decision evidence"
        payload = _json.loads(policy_events[0].canonical_payload)
        assert payload["execution_intent_id"] == "unset"
        assert payload["approval_correlation_id"] == "unset"

    def test_principal_authority_is_immutable_per_execute(self, monkeypatch):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)
        call_count = {"n": 0}

        def governed_echo(text: str) -> str:
            call_count["n"] += 1
            return text

        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
        )
        orch.register_governed_handler("builtin.echo_text.in_process", governed_echo)
        ctx_a = orch.build_authenticated_principal_context(
            principal_identity="user:a", principal_scopes=["scope.a"]
        )
        preview = orch.preview(
            TaskManifold(objective="test", t_max_steps=3), principal_context=ctx_a
        )
        digest = preview["action_digest"]

        original_scope_check = orch._check_principal_scopes

        def _mutating_scope_check(governed_entry, principal_authority=None):
            orch.set_authenticated_principal_context(
                principal_identity="user:b", principal_scopes=["scope.b"]
            )
            return original_scope_check(governed_entry, principal_authority)

        monkeypatch.setattr(orch, "_check_principal_scopes", _mutating_scope_check)
        receipt = orch.execute(
            TaskManifold(
                objective="test",
                t_max_steps=3,
                metadata={"approved_action_digest": digest},
            ),
            principal_context=ctx_a,
        )
        assert receipt.halt_reason != "APPROVED_ACTION_MISMATCH"
        assert call_count["n"] >= 1

    def test_pre_entry_principal_interleaving_cannot_swap_authority_with_per_call_context(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)
        calls = {"n": 0}

        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
        )
        orch.register_governed_handler(
            "builtin.echo_text.in_process",
            lambda text: calls.__setitem__("n", calls["n"] + 1) or text,
        )
        ctx_a = orch.build_authenticated_principal_context(
            principal_identity="user:a", principal_scopes=["scope.a"]
        )
        ctx_b = orch.build_authenticated_principal_context(
            principal_identity="user:b", principal_scopes=["scope.b"]
        )

        preview = orch.preview(
            TaskManifold(objective="test", t_max_steps=3), principal_context=ctx_a
        )
        digest = preview["action_digest"]
        orch.set_authenticated_principal_context(
            principal_identity=ctx_b.principal_identity,
            principal_scopes=list(ctx_b.principal_scopes),
        )
        receipt = orch.execute(
            TaskManifold(
                objective="test",
                t_max_steps=3,
                metadata={"approved_action_digest": digest},
            ),
            principal_context=ctx_a,
        )
        assert receipt.halt_reason != "APPROVED_ACTION_MISMATCH"
        assert calls["n"] == 1

    def test_evaluator_identity_drift_invalidates_stale_approval(self, monkeypatch, tmp_path):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.policy_engine import OpaMode, PolicyEngine
        from sovereign_claw.thermodynamics import TaskManifold

        policy_dir = tmp_path / "policy"
        policy_dir.mkdir()
        (policy_dir / "policy.rego").write_text("package sovereign_claw\nallow := true\n")

        current_evaluator = {"id": "evaluator-A"}
        policy_engine = PolicyEngine(rego_policy_dir=policy_dir, opa_mode=OpaMode.AUTHORITATIVE)
        monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: sys.executable)
        monkeypatch.setattr(policy_engine, "_local_evaluator_identity", lambda: "local-id")
        monkeypatch.setattr(
            policy_engine,
            "_resolve_opa_evaluator_identity",
            lambda: (Path(sys.executable), current_evaluator["id"]),
        )
        monkeypatch.setattr(
            policy_engine,
            "_snapshot_opa_evaluator",
            lambda: policy_engine_module._EvaluatorSnapshot(
                binary_path=Path(sys.executable),
                identity=current_evaluator["id"],
                cleanup_handle=None,
            ),
        )
        monkeypatch.setattr(
            policy_engine,
            "_run_bounded_subprocess",
            lambda **kwargs: {
                "returncode": 0,
                "stdout": _json.dumps(
                    {
                        "result": [
                            {"expressions": [{"value": {"allow": True, "deny": [], "matched": []}}]}
                        ]
                    }
                ).encode("utf-8"),
                "stderr": b"",
                "stdout_overflow": False,
                "stderr_overflow": False,
                "timed_out": False,
                "stdin_error": False,
            },
        )

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)
        calls = {"n": 0}
        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
            policy_engine=policy_engine,
        )
        orch.register_governed_handler(
            "builtin.echo_text.in_process",
            lambda text: calls.__setitem__("n", calls["n"] + 1) or text,
        )

        preview = orch.preview(TaskManifold(objective="test", t_max_steps=3))
        digest = preview["action_digest"]
        current_evaluator["id"] = "evaluator-B"
        receipt = orch.execute(
            TaskManifold(
                objective="test",
                t_max_steps=3,
                metadata={"approved_action_digest": digest},
            )
        )
        assert receipt.halt_reason == "APPROVED_ACTION_MISMATCH"
        assert calls["n"] == 0


# ── Part 13: Fix 3 — max_output_bytes enforcement ────────────────────────────


def _make_spec_with_tiny_output_cap(tool_id: str) -> ToolSpecV1:
    """Return a ToolSpecV1 with max_output_bytes=10."""
    return ToolSpecV1(
        schema_version="1",
        tool_id=tool_id,
        tool_version="1.0.0",
        description_hash=sha256_hex(b"tiny cap spec"),
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
        worker_handler_id=f"{tool_id}.handler",
        worker_build_identity="IN_PROCESS",
        default_deadline_ms=5_000,
        max_deadline_ms=30_000,
        max_input_bytes=64 * 1024,
        max_output_bytes=10,  # tiny cap
        reversibility="reversible",
        idempotency="idempotent",
        postcondition_validator_id="",
        postcondition_validator_version="",
        evidence_policy="digest_only",
        redaction_policy="default",
    )


class TestMaxOutputBytesEnforcement:
    """Fix 3: max_output_bytes must be enforced before success."""

    def test_oversized_output_cannot_report_success(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        spec = _make_spec_with_tiny_output_cap("test.big_output")
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(tool="test.big_output", kwargs={"text": "hi"}),
            tool_registry=registry,
        )
        # Return a long string guaranteed to exceed 10 bytes
        orch.register_governed_handler("test.big_output.handler", lambda text: "X" * 200)

        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))
        assert receipt.halt_reason is not None
        # Must be an output-related failure
        reason = receipt.halt_reason.lower()
        assert "output" in reason or "schema" in reason or "size" in reason

    def test_within_limit_does_not_fail(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        spec = _make_spec_with_tiny_output_cap("test.small_output")
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(tool="test.small_output", kwargs={"text": "hi"}),
            tool_registry=registry,
        )
        # Return a 1-byte string — well within 10-byte cap
        orch.register_governed_handler("test.small_output.handler", lambda text: "X")

        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))
        assert "OUTPUT_SCHEMA_INVALID" not in (receipt.halt_reason or "")


class TestPolicyAuthorityFollowupRegressions:
    def test_opa_runner_limit_change_invalidates_stale_approval(self, monkeypatch, tmp_path):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.policy_engine import OpaMode, PolicyEngine
        from sovereign_claw.thermodynamics import TaskManifold

        policy_dir = tmp_path / "policy"
        policy_dir.mkdir()
        (policy_dir / "policy.rego").write_text("package sovereign_claw\nallow := true\n")

        policy_engine = PolicyEngine(
            rego_policy_dir=policy_dir,
            opa_mode=OpaMode.AUTHORITATIVE,
            opa_timeout_ms=500,
        )
        monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: sys.executable)
        monkeypatch.setattr(policy_engine, "_local_evaluator_identity", lambda: "local-id")
        monkeypatch.setattr(
            policy_engine,
            "_resolve_opa_evaluator_identity",
            lambda: (Path(sys.executable), "eval-id"),
        )
        monkeypatch.setattr(
            policy_engine,
            "_snapshot_opa_evaluator",
            lambda: policy_engine_module._EvaluatorSnapshot(
                binary_path=Path(sys.executable),
                identity="eval-id",
                cleanup_handle=None,
            ),
        )
        monkeypatch.setattr(
            policy_engine,
            "_run_bounded_subprocess",
            lambda **kwargs: {
                "returncode": 0,
                "stdout": _json.dumps(
                    {
                        "result": [
                            {"expressions": [{"value": {"allow": True, "deny": [], "matched": []}}]}
                        ]
                    }
                ).encode("utf-8"),
                "stderr": b"",
                "stdout_overflow": False,
                "stderr_overflow": False,
                "timed_out": False,
                "stdin_error": False,
            },
        )

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)
        calls = {"n": 0}
        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
            policy_engine=policy_engine,
        )
        orch.register_governed_handler(
            "builtin.echo_text.in_process",
            lambda text: calls.__setitem__("n", calls["n"] + 1) or text,
        )
        principal_ctx = orch.build_authenticated_principal_context(
            principal_identity="user:a", principal_scopes=[]
        )
        preview = orch.preview(
            TaskManifold(objective="test", t_max_steps=3), principal_context=principal_ctx
        )
        digest = preview["action_digest"]
        policy_engine.opa_timeout_ms = 900
        receipt = orch.execute(
            TaskManifold(
                objective="test", t_max_steps=3, metadata={"approved_action_digest": digest}
            ),
            principal_context=principal_ctx,
        )
        assert receipt.halt_reason == "APPROVED_ACTION_MISMATCH"
        assert calls["n"] == 0

    def test_spoofed_provider_claim_does_not_override_demo_backend_authority(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.policy_engine import PolicyEngine, PolicyProfile
        from sovereign_claw.thermodynamics import TaskManifold

        class DemoBackend:
            def decide_next_action(self, objective, history, forbidden_actions, drift):
                return {
                    "tool": "builtin.echo_text",
                    "kwargs": {"text": "hello"},
                    "comment": "",
                    "provider": "trusted",
                    "agent_id": "demo_backend",
                }

        registry = ToolRegistry()
        registry.register(make_registry_entry(TOOL_SPEC_V1_ECHO))
        policy_engine = PolicyEngine(profile=PolicyProfile.STRICT)
        calls = {"n": 0}
        orch = Orchestrator(
            llm_backend=DemoBackend(),
            tool_registry=registry,
            policy_engine=policy_engine,
        )
        orch.register_governed_handler(
            "builtin.echo_text.in_process",
            lambda text: calls.__setitem__("n", calls["n"] + 1) or text,
        )
        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))
        assert receipt.halt_reason is not None
        assert "demo backend not allowed" in receipt.halt_reason.lower()
        assert calls["n"] == 0

    def test_policy_bundle_hash_mismatch_persists_policy_decision_evidence(self, monkeypatch):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.proof_vault import ProofVault
        from sovereign_claw.thermodynamics import TaskManifold

        registry = ToolRegistry()
        registry.register(make_registry_entry(TOOL_SPEC_V1_ECHO))
        vault = ProofVault()
        calls = {"n": 0}
        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
            vault=vault,
        )
        orch.register_governed_handler(
            "builtin.echo_text.in_process",
            lambda text: calls.__setitem__("n", calls["n"] + 1) or text,
        )
        principal_ctx = orch.build_authenticated_principal_context(
            principal_identity="user:a", principal_scopes=[]
        )
        original_eval = orch.policy_engine.evaluate_context

        def _mismatch(*args, **kwargs):
            decision = original_eval(*args, **kwargs)
            decision.policy_bundle_hash = "0" * 64
            return decision

        monkeypatch.setattr(orch.policy_engine, "evaluate_context", _mismatch)
        receipt = orch.execute(
            TaskManifold(objective="test", t_max_steps=3), principal_context=principal_ctx
        )
        assert receipt.halt_reason == "POLICY_BUNDLE_HASH_MISMATCH"
        assert calls["n"] == 0

        policy_events = [
            rec
            for rec in vault.get_evidence_records(receipt.trace_id)
            if rec.evidence_type == "authority.policy.decision"
        ]
        assert policy_events
        payload = _json.loads(policy_events[-1].canonical_payload)
        assert payload["decision_class"] == "POLICY_INFRA_FAILURE"
        assert payload["expected_policy_bundle_hash"]
        assert payload["evaluated_policy_bundle_hash"] == "0" * 64

    def test_policy_runtime_failure_persists_policy_decision_evidence(self, monkeypatch):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.proof_vault import ProofVault
        from sovereign_claw.thermodynamics import TaskManifold

        registry = ToolRegistry()
        registry.register(make_registry_entry(TOOL_SPEC_V1_ECHO))
        vault = ProofVault()
        calls = {"n": 0}
        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
            vault=vault,
        )
        orch.register_governed_handler(
            "builtin.echo_text.in_process",
            lambda text: calls.__setitem__("n", calls["n"] + 1) or text,
        )
        principal_ctx = orch.build_authenticated_principal_context(
            principal_identity="user:a", principal_scopes=[]
        )
        monkeypatch.setattr(
            orch.policy_engine,
            "evaluate_context",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        receipt = orch.execute(
            TaskManifold(objective="test", t_max_steps=3), principal_context=principal_ctx
        )
        assert receipt.halt_reason == "Policy engine failure: RuntimeError"
        assert calls["n"] == 0
        policy_events = [
            rec
            for rec in vault.get_evidence_records(receipt.trace_id)
            if rec.evidence_type == "authority.policy.decision"
        ]
        assert policy_events
        payload = _json.loads(policy_events[-1].canonical_payload)
        assert payload["decision_class"] == "POLICY_INFRA_FAILURE"
        assert payload["allowed"] is False

    def test_policy_bundle_component_identities_are_persisted(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.proof_vault import ProofVault
        from sovereign_claw.thermodynamics import TaskManifold

        registry = ToolRegistry()
        registry.register(make_registry_entry(TOOL_SPEC_V1_ECHO))
        vault = ProofVault()
        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
            vault=vault,
        )
        orch.register_governed_handler("builtin.echo_text.in_process", lambda text: text)
        principal_ctx = orch.build_authenticated_principal_context(
            principal_identity="user:a", principal_scopes=[]
        )
        receipt = orch.execute(
            TaskManifold(objective="test", t_max_steps=3), principal_context=principal_ctx
        )
        policy_events = [
            rec
            for rec in vault.get_evidence_records(receipt.trace_id)
            if rec.evidence_type == "authority.policy.decision"
        ]
        assert policy_events
        payload = _json.loads(policy_events[-1].canonical_payload)
        bundle = payload["bundle_components"]
        assert bundle["local_rules_hash"]
        assert bundle["guardrail_bundle_identity"]
        assert bundle["learned_signal_mode"]
        assert bundle["learned_signal_root"] == "none"

    def test_caller_t_max_steps_is_non_authoritative_in_budget_state(self, monkeypatch):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        registry = ToolRegistry()
        registry.register(make_registry_entry(TOOL_SPEC_V1_ECHO))
        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
        )
        orch.register_governed_handler("builtin.echo_text.in_process", lambda text: text)
        principal_ctx = orch.build_authenticated_principal_context(
            principal_identity="user:a", principal_scopes=[]
        )

        seen = []
        original_eval = orch.policy_engine.evaluate_context

        def _capture(context, **kwargs):
            seen.append(context)
            return original_eval(context, **kwargs)

        monkeypatch.setattr(orch.policy_engine, "evaluate_context", _capture)
        orch.execute(TaskManifold(objective="test", t_max_steps=2), principal_context=principal_ctx)
        orch.llm._called = 0  # type: ignore[attr-defined]
        orch.execute(
            TaskManifold(objective="test", t_max_steps=50), principal_context=principal_ctx
        )
        assert len(seen) >= 2
        assert seen[0].budget_state["remaining_steps"] == "unset"
        assert seen[1].budget_state["remaining_steps"] == "unset"
        assert seen[0].model_claims["caller_t_max_steps"] == 2
        assert seen[1].model_claims["caller_t_max_steps"] == 50

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"text": float("nan")},
            {"text": [[[["x"]]] * 100]},
        ],
    )
    def test_execute_rejects_nonfinite_or_deep_model_output_before_policy_serialization(
        self, kwargs
    ):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        class _Backend:
            def decide_next_action(self, objective, history, forbidden_actions, drift):
                return {
                    "tool": "builtin.echo_text",
                    "kwargs": kwargs,
                    "comment": "",
                    "agent_id": "x",
                }

        registry = ToolRegistry()
        registry.register(make_registry_entry(TOOL_SPEC_V1_ECHO))
        calls = {"n": 0}
        orch = Orchestrator(llm_backend=_Backend(), tool_registry=registry)
        orch.register_governed_handler(
            "builtin.echo_text.in_process",
            lambda text: calls.__setitem__("n", calls["n"] + 1) or text,
        )
        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))
        assert receipt.halt_reason is not None
        assert calls["n"] == 0

    def test_execute_rejects_cyclic_model_output_before_policy_serialization(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        cyclic: list[Any] = []
        cyclic.append(cyclic)

        class _Backend:
            def decide_next_action(self, objective, history, forbidden_actions, drift):
                return {
                    "tool": "builtin.echo_text",
                    "kwargs": {"text": cyclic},
                    "comment": "",
                    "agent_id": "x",
                }

        registry = ToolRegistry()
        registry.register(make_registry_entry(TOOL_SPEC_V1_ECHO))
        calls = {"n": 0}
        orch = Orchestrator(llm_backend=_Backend(), tool_registry=registry)
        orch.register_governed_handler(
            "builtin.echo_text.in_process",
            lambda text: calls.__setitem__("n", calls["n"] + 1) or text,
        )
        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))
        assert receipt.halt_reason is not None
        assert calls["n"] == 0


# ── Part 14: Fix 4 — Postcondition validator enforcement ─────────────────────


def _make_spec_with_postcondition(
    tool_id: str,
    validator_id: str,
    validator_version: str = "1.0.0",
) -> ToolSpecV1:
    return ToolSpecV1(
        schema_version="1",
        tool_id=tool_id,
        tool_version="1.0.0",
        description_hash=sha256_hex(b"postcond spec"),
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
        worker_handler_id=f"{tool_id}.handler",
        worker_build_identity="IN_PROCESS",
        default_deadline_ms=5_000,
        max_deadline_ms=30_000,
        max_input_bytes=64 * 1024,
        max_output_bytes=256 * 1024,
        reversibility="reversible",
        idempotency="idempotent",
        postcondition_validator_id=validator_id,
        postcondition_validator_version=validator_version,
        evidence_policy="digest_only",
        redaction_policy="default",
    )


class TestPostconditionValidatorEnforcement:
    """Fix 4: Declared postconditions must be real authority."""

    def test_missing_validator_registry_cannot_report_success(self):
        """Spec declares a validator but Orchestrator has no registry → halt."""
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        spec = _make_spec_with_postcondition("test.req_validator", "test.postcond.v1")
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        call_count = {"n": 0}

        def fn(text: str) -> str:
            call_count["n"] += 1
            return text

        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(tool="test.req_validator", kwargs={"text": "hi"}),
            tool_registry=registry,
            # No postcondition_validator_registry
        )
        orch.register_governed_handler("test.req_validator.handler", fn)

        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))
        assert call_count["n"] >= 1, "tool should have run before postcondition check"
        assert "POSTCONDITION_FAILED" in (receipt.halt_reason or "")

    def test_unregistered_validator_id_cannot_report_success(self):
        """Declared validator_id not in registry → MissingPostconditionValidatorError."""
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        spec = _make_spec_with_postcondition("test.missing_val", "test.no_such_validator")
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        pv_registry = PostconditionValidatorRegistry()
        # Do NOT register "test.no_such_validator"

        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(tool="test.missing_val", kwargs={"text": "hi"}),
            tool_registry=registry,
            postcondition_validator_registry=pv_registry,
        )
        orch.register_governed_handler("test.missing_val.handler", lambda text: text)

        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))
        assert "POSTCONDITION_FAILED" in (receipt.halt_reason or "")

    def test_failing_validator_cannot_report_success(self):
        """A validator that raises PostconditionFailedError must halt execution."""
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        spec = _make_spec_with_postcondition("test.fail_val", "test.always_fail_v1")
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        pv_registry = PostconditionValidatorRegistry()

        def always_fail(kwargs: Any, output: Any, metadata: Any) -> None:
            raise PostconditionFailedError("postcondition always fails")

        pv_registry.register("test.always_fail_v1", "1.0.0", always_fail)

        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(tool="test.fail_val", kwargs={"text": "hi"}),
            tool_registry=registry,
            postcondition_validator_registry=pv_registry,
        )
        orch.register_governed_handler("test.fail_val.handler", lambda text: text)

        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))
        assert "POSTCONDITION_FAILED" in (receipt.halt_reason or "")

    def test_write_json_postcondition_validator_registered_in_builtin(self):
        """TOOL_SPEC_V1_WRITE_JSON must have a real bound postcondition validator."""
        assert TOOL_SPEC_V1_WRITE_JSON.postcondition_validator_id != ""
        fn = BUILTIN_POSTCONDITION_VALIDATORS.get(
            TOOL_SPEC_V1_WRITE_JSON.postcondition_validator_id,
            TOOL_SPEC_V1_WRITE_JSON.postcondition_validator_version,
        )
        assert fn is not None

    def test_write_json_postcondition_passes_on_correct_write(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path)
        data = {"verified": True, "n": 42}
        scoped_write_json_file(root_id, "ok.json", data)
        fn = BUILTIN_POSTCONDITION_VALIDATORS.get("builtin.write_json_file.digest_check", "1.0.0")
        assert fn is not None
        fn({"root_id": root_id, "relative_path": "ok.json", "data": data}, "ok.json", {})

    def test_write_json_postcondition_fails_on_tampered_file(self, tmp_path):
        root_id = create_filesystem_capability(tmp_path, allow_overwrite=True)
        data = {"original": True}
        scoped_write_json_file(root_id, "tampered.json", data)
        (tmp_path / "tampered.json").write_bytes(b'{"tampered":true}')
        fn = BUILTIN_POSTCONDITION_VALIDATORS.get("builtin.write_json_file.digest_check", "1.0.0")
        assert fn is not None
        with pytest.raises(PostconditionFailedError, match="digest mismatch"):
            fn(
                {"root_id": root_id, "relative_path": "tampered.json", "data": data},
                "tampered.json",
                {},
            )


# ── Part 15: Fix 5 — Evidence persistence failure ────────────────────────────


class TestEvidencePersistenceFailure:
    """Fix 5: ProofVault evidence failure after actuation must prevent success."""

    def test_policy_decision_evidence_failure_blocks_before_actuation(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.proof_vault import ProofVault
        from sovereign_claw.thermodynamics import TaskManifold

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        vault = ProofVault()
        tool_calls = {"count": 0}

        def governed_echo(text: str) -> str:
            tool_calls["count"] += 1
            return text

        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(tool="builtin.echo_text", kwargs={"text": "hi"}),
            tool_registry=registry,
            vault=vault,
        )
        orch.register_governed_handler("builtin.echo_text.in_process", governed_echo)

        original_append = vault.append_authority_event

        def fail_policy_decision(event_type: str, trace_id: str, payload: dict, *, timestamp=None):
            if event_type == "policy.decision":
                raise RuntimeError("policy evidence fail")
            return original_append(event_type, trace_id, payload, timestamp=timestamp)

        from unittest.mock import patch

        with patch.object(vault, "append_authority_event", side_effect=fail_policy_decision):
            receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))

        assert receipt.halt_reason == "EVIDENCE_PERSISTENCE_FAILED"
        assert tool_calls["count"] == 0

    def test_evidence_persistence_failure_reports_uncertain_outcome(self):
        """append_authority_event failure after tool actuation → EVIDENCE_PERSISTENCE_FAILED."""
        from unittest.mock import patch

        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.proof_vault import ProofVault
        from sovereign_claw.thermodynamics import TaskManifold

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        vault = ProofVault()
        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(tool="builtin.echo_text", kwargs={"text": "hi"}),
            tool_registry=registry,
            vault=vault,
        )
        orch.register_governed_handler("builtin.echo_text.in_process", lambda text: text)

        with patch.object(vault, "append_authority_event", side_effect=RuntimeError("db failure")):
            receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))

        assert receipt.halt_reason == "EVIDENCE_PERSISTENCE_FAILED"

    def test_evidence_persistence_error_class_is_stable(self):
        err = EvidencePersistenceFailedError("x")
        assert err.error_code == "EVIDENCE_PERSISTENCE_FAILED"
        assert isinstance(err, ToolAuthorityError)


# ── Part 16: Fix 6 — Privacy-safe step payloads ───────────────────────────────


class TestPrivacySafeStepPayloads:
    """Fix 6: Governed step records must not log raw kwargs/results."""

    def test_governed_step_record_has_no_raw_kwargs_or_result(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.proof_vault import ProofVault
        from sovereign_claw.thermodynamics import TaskManifold

        secret_value = "ultra_secret_governed_value_xyz_9273"
        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        vault = ProofVault()
        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(
                tool="builtin.echo_text",
                kwargs={"text": secret_value},
            ),
            tool_registry=registry,
            vault=vault,
        )
        orch.register_governed_handler("builtin.echo_text.in_process", lambda text: text)
        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))

        all_records = vault.get_evidence_records(receipt.trace_id)
        for rec in all_records:
            assert secret_value not in rec.canonical_payload, (
                f"Secret value leaked into {rec.evidence_type} record"
            )

    def test_governed_step_record_has_metadata_not_raw_payload(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.proof_vault import ProofVault
        from sovereign_claw.thermodynamics import TaskManifold

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        vault = ProofVault()
        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(tool="builtin.echo_text", kwargs={"text": "hello"}),
            tool_registry=registry,
            vault=vault,
        )
        orch.register_governed_handler("builtin.echo_text.in_process", lambda text: text)
        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))

        all_records = vault.get_evidence_records(receipt.trace_id)
        step_records = [r for r in all_records if r.evidence_type.startswith("step")]
        assert len(step_records) >= 1
        for rec in step_records:
            payload = _json.loads(rec.canonical_payload)
            # Governed step must use bounded metadata
            if "tool_id" in payload:
                assert "tool_contract_hash" in payload
                assert "action_digest" in payload
                assert "canonical_args_digest" in payload
                assert "tool_kwargs" not in payload, "raw kwargs must not be logged"
                assert "tool_result" not in payload, "raw result must not be logged"

    def test_authority_event_has_no_raw_output_value(self):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.proof_vault import ProofVault
        from sovereign_claw.thermodynamics import TaskManifold

        secret_output = "secret_file_body_content_abc_def"

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        vault = ProofVault()
        orch = Orchestrator(
            llm_backend=_EchoBackendScopeTest(
                tool="builtin.echo_text", kwargs={"text": secret_output}
            ),
            tool_registry=registry,
            vault=vault,
        )
        orch.register_governed_handler("builtin.echo_text.in_process", lambda text: text)
        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=3))

        all_records = vault.get_evidence_records(receipt.trace_id)
        authority_events = [r for r in all_records if r.evidence_type == "authority.tool.execution"]
        assert len(authority_events) >= 1
        for rec in authority_events:
            assert secret_output not in rec.canonical_payload, (
                "Raw output body must not appear in authority event"
            )


# ── Part 17: Fix 1 — worker_handler_id authority binding regression ───────────


class TestWorkerHandlerIdAuthorityBinding:
    """
    Fix 1 (part 2): Dispatch must be keyed by exact worker_handler_id, not tool_id.
    A callable bound under one worker_handler_id must never be reachable via a
    different/forged handler_id, and a forged entry with a different worker_handler_id
    cannot reuse a callable that was bound under the canonical handler_id.
    """

    def test_forged_handler_id_cannot_dispatch_canonical_callable(self):
        """
        Bind a callable under a forged handler_id that does NOT match the spec's
        worker_handler_id.  Dispatch must fail closed — the callable never runs.

        Attack model: attacker binds a callable under an arbitrary handler_id (not the
        canonical worker_handler_id for the tool_id) hoping dispatch will route to it.
        The registry-keyed lookup must prevent this.
        """
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        call_log: dict[str, int] = {"forged": 0}

        def forged_fn(text: str) -> str:
            call_log["forged"] += 1
            return text

        # Canonical entry: tool_id="builtin.echo_text", handler_id="builtin.echo_text.in_process"
        canonical_entry = make_registry_entry(TOOL_SPEC_V1_ECHO)
        registry = ToolRegistry()
        registry.register(canonical_entry)

        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hi"}),
            tool_registry=registry,
        )
        # Bind the callable under a FORGED handler_id — NOT the canonical worker_handler_id
        forged_handler_id = "forged.not.canonical.handler.id"
        assert forged_handler_id != canonical_entry.worker_handler_id
        orch.register_governed_handler(forged_handler_id, forged_fn)

        # Dispatch must fail closed — forged callable must never run
        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=5))
        assert call_log["forged"] == 0, "callable bound under forged handler_id must never run"
        assert receipt.halt_reason is not None, "execution must fail closed"

    def test_missing_handler_binding_fails_closed_zero_calls(self):
        """
        If the canonical worker_handler_id is never bound via register_governed_handler,
        governed dispatch must halt with zero tool calls (GOVERNED_HANDLER_NOT_FOUND).
        """
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.thermodynamics import TaskManifold

        call_log: dict[str, int] = {"n": 0}

        def fn(text: str) -> str:
            call_log["n"] += 1
            return text

        entry = make_registry_entry(TOOL_SPEC_V1_ECHO)
        registry = ToolRegistry()
        registry.register(entry)

        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "hi"}),
            tool_registry=registry,
        )
        # Intentionally bind under a WRONG handler_id (not the spec's worker_handler_id)
        orch.register_governed_handler("wrong.handler.id", fn)

        receipt = orch.execute(TaskManifold(objective="test", t_max_steps=5))
        assert call_log["n"] == 0, "callable bound under wrong handler_id must never run"
        assert (
            "GOVERNED_HANDLER_NOT_FOUND" in (receipt.halt_reason or "")
            or receipt.halt_reason is not None
        ), "must fail closed"


# ── Part 18: Fix 2 — register_all contract-conflict fail-closed regression ────


class TestRegisterAllContractConflictFailClosed:
    """
    Fix 2 (part 2): register_all() must propagate DuplicateToolRegistrationError when
    a tool_id is pre-registered with a DIFFERENT contract hash (contract conflict).
    Identical re-registration remains idempotent (no exception).
    """

    def test_conflicting_contract_registration_propagates(self):
        """
        Pre-register the same tool_id with a different contract, then call register_all().
        The conflicting registration MUST raise, not be swallowed.
        """
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.tool_authority import DuplicateToolRegistrationError

        class _DummyLLM:
            def decide_next_action(self, *args, **kwargs):
                return {"tool": "HALT", "kwargs": {}, "comment": ""}

        registry = ToolRegistry()

        # Pre-register "builtin.echo_text" with a tampered spec (different description_hash)
        tampered_spec = ToolSpecV1(
            schema_version="1",
            tool_id="builtin.echo_text",
            tool_version="1.0.0",
            description_hash=sha256_hex(b"tampered description"),  # different hash
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
            max_input_bytes=4 * 1024,
            max_output_bytes=256 * 1024,
            reversibility="reversible",
            idempotency="idempotent",
            postcondition_validator_id="",
            postcondition_validator_version="",
            evidence_policy="digest_only",
            redaction_policy="default",
        )
        tampered_entry = make_registry_entry(tampered_spec)
        registry.register(tampered_entry)

        orch = Orchestrator(llm_backend=_DummyLLM(), tool_registry=registry)
        # register_all must raise, not silently continue
        with pytest.raises(DuplicateToolRegistrationError):
            register_all(orch)

    def test_identical_reregistration_remains_idempotent(self):
        """
        Calling register_all() twice with the same specs must not raise.
        """
        from sovereign_claw.orchestrator import Orchestrator

        class _DummyLLM:
            def decide_next_action(self, *args, **kwargs):
                return {"tool": "HALT", "kwargs": {}, "comment": ""}

        registry = ToolRegistry()
        orch = Orchestrator(llm_backend=_DummyLLM(), tool_registry=registry)
        register_all(orch)
        register_all(orch)  # must not raise


# ── Part 20: Sanitized governed-evidence privacy tests ───────────────────────


# ── Part 20: Sanitized governed-evidence privacy tests ───────────────────────


class TestSanitizedFailureRecordPrivacy:
    """
    Adversarial tests: secret sentinel strings must never appear in
    get_evidence_records() / exported governed evidence after output-schema
    or postcondition failures.  Only error_class/digest/bytes must be present.
    """

    # ── helpers ──────────────────────────────────────────────────────────────

    def _make_orch_and_vault(self, spec, handler_fn, llm_backend, pv_registry=None):
        from sovereign_claw.orchestrator import Orchestrator
        from sovereign_claw.proof_vault import ProofVault

        registry = ToolRegistry()
        registry.register(make_registry_entry(spec))
        vault = ProofVault()
        orch = Orchestrator(
            llm_backend=llm_backend,
            tool_registry=registry,
            vault=vault,
            postcondition_validator_registry=pv_registry,
        )
        orch.register_governed_handler(spec.worker_handler_id, handler_fn)
        return orch, vault

    @staticmethod
    def _run_manifold():
        from sovereign_claw.thermodynamics import TaskManifold

        return TaskManifold(objective="test", t_max_steps=5)

    # ── output-schema failure leaks no secret ────────────────────────────────

    def test_output_schema_failure_secret_never_in_evidence(self):
        """
        When validate_output() raises because the output type is wrong,
        the raw diagnostic (which may embed the actual output value) must not
        appear in any evidence record.  error_class/digest/bytes must be present.
        """
        secret_sentinel = "TOPLEVEL_SECRET_OUTPUT_VALUE_abc123xyz"

        # Spec expects an integer output; handler returns the secret string.
        spec = _make_spec(
            tool_id="test.secret_output",
            worker_handler_id="test.secret_output.handler",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema={"type": "integer"},
        )

        class _LLM:
            def decide_next_action(self, *a, **kw):
                return {"tool": "test.secret_output", "kwargs": {}, "comment": ""}

        # Handler returns the sentinel; validate_output will embed it in the error message.
        orch, vault = self._make_orch_and_vault(spec, lambda: secret_sentinel, _LLM())
        receipt = orch.execute(self._run_manifold())

        assert receipt.halt_reason is not None
        assert "OUTPUT_SCHEMA_INVALID" in receipt.halt_reason

        # The sentinel must not appear in any evidence record
        all_records = vault.get_evidence_records(receipt.trace_id)
        for rec in all_records:
            assert secret_sentinel not in rec.canonical_payload, (
                f"Secret sentinel leaked into evidence record type={rec.evidence_type!r}"
            )

        # Authority events must carry sanitized failure metadata (class+digest+bytes)
        authority_events = [r for r in all_records if r.evidence_type == "authority.tool.execution"]
        assert len(authority_events) >= 1
        for rec in authority_events:
            payload = _json.loads(rec.canonical_payload)
            failure = payload.get("output_schema_failure")
            assert failure is not None, "output_schema_failure must be present in authority event"
            assert "error_class" in failure
            assert "diagnostic_digest" in failure
            assert isinstance(failure["diagnostic_bytes"], int)
            assert failure["diagnostic_bytes"] > 0

    # ── postcondition failure leaks no secret ────────────────────────────────

    def test_postcondition_failure_secret_never_in_evidence(self):
        """
        When the postcondition validator raises an exception containing a
        secret path/value, the raw exception message must not appear in any
        evidence record.  error_class/digest/bytes must be present.
        """
        secret_sentinel = "SECRET_PATH_OR_VALUE_xyz789postcond"

        spec = _make_spec(
            tool_id="test.secret_postcond",
            worker_handler_id="test.secret_postcond.handler",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )
        # Override postcondition fields manually
        spec = ToolSpecV1(
            schema_version=spec.schema_version,
            tool_id=spec.tool_id,
            tool_version=spec.tool_version,
            description_hash=spec.description_hash,
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
            capabilities=spec.capabilities,
            risk_class=spec.risk_class,
            required_principal_scopes=spec.required_principal_scopes,
            isolation_profile=spec.isolation_profile,
            worker_handler_id=spec.worker_handler_id,
            worker_build_identity=spec.worker_build_identity,
            default_deadline_ms=spec.default_deadline_ms,
            max_deadline_ms=spec.max_deadline_ms,
            max_input_bytes=spec.max_input_bytes,
            max_output_bytes=spec.max_output_bytes,
            reversibility=spec.reversibility,
            idempotency=spec.idempotency,
            postcondition_validator_id="test.secret_postcond.validator",
            postcondition_validator_version="1",
            evidence_policy=spec.evidence_policy,
            redaction_policy=spec.redaction_policy,
        )

        class _LLM:
            def decide_next_action(self, *a, **kw):
                return {"tool": "test.secret_postcond", "kwargs": {}, "comment": ""}

        def _validator(validator_id, version, kwargs, result, context):
            raise PostconditionFailedError(
                f"validation failed: {secret_sentinel} is missing from disk"
            )

        pv_registry = PostconditionValidatorRegistry()
        pv_registry.register("test.secret_postcond.validator", "1", _validator)

        orch, vault = self._make_orch_and_vault(spec, lambda: "ok", _LLM(), pv_registry)
        receipt = orch.execute(self._run_manifold())

        assert receipt.halt_reason is not None
        assert "POSTCONDITION_FAILED" in receipt.halt_reason

        # The sentinel must not appear in any evidence record
        all_records = vault.get_evidence_records(receipt.trace_id)
        for rec in all_records:
            assert secret_sentinel not in rec.canonical_payload, (
                f"Secret sentinel leaked into evidence record type={rec.evidence_type!r}"
            )

        # Authority events must carry sanitized failure metadata (class+digest+bytes)
        authority_events = [r for r in all_records if "authority" in r.evidence_type]
        assert len(authority_events) >= 1
        failures = []
        for rec in authority_events:
            payload = _json.loads(rec.canonical_payload)
            failure = payload.get("postcondition_failure")
            if failure is not None:
                failures.append(failure)
        assert failures, "postcondition_failure must be present in authority event"
        for failure in failures:
            assert "error_class" in failure
            assert "diagnostic_digest" in failure
            assert isinstance(failure["diagnostic_bytes"], int)
            assert failure["diagnostic_bytes"] > 0

    # ── halt_reason itself must not embed the raw diagnostic ─────────────────

    def test_halt_reason_does_not_contain_raw_diagnostic(self):
        """
        The halt_reason string stored in ExecutionReceipt must include the
        stable error class and digest prefix, but must not contain the raw
        diagnostic body (which may carry secrets).
        """
        secret_sentinel = "HALT_REASON_SECRET_sentinel_99"

        spec = _make_spec(
            tool_id="test.halt_reason_privacy",
            worker_handler_id="test.halt_reason_privacy.handler",
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema={"type": "integer"},
        )

        class _LLM:
            def decide_next_action(self, *a, **kw):
                return {"tool": "test.halt_reason_privacy", "kwargs": {}, "comment": ""}

        orch, _vault = self._make_orch_and_vault(spec, lambda: secret_sentinel, _LLM())
        receipt = orch.execute(self._run_manifold())

        assert "OUTPUT_SCHEMA_INVALID" in receipt.halt_reason
        assert secret_sentinel not in receipt.halt_reason, (
            "halt_reason must not contain the raw diagnostic body"
        )
        # Must contain a stable class and digest reference
        assert "digest=" in receipt.halt_reason
