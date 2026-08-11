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

import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

from sovereign_claw.tool_authority import (
    ACTION_VERSION,
    ApprovedActionMismatchError,
    CyclicValueError,
    DuplicateToolRegistrationError,
    HandlerSubstitutionError,
    InputSchemaInvalidError,
    InvalidSchemaError,
    InvalidSpecFieldError,
    NonStringKeyError,
    OutputSchemaInvalidError,
    RawCallableAuthorityError,
    ToolAuthorityError,
    ToolContractChangedError,
    ToolRegistry,
    ToolRegistryEntry,
    ToolSpecV1,
    UnknownToolError,
    UnsupportedValueTypeError,
    canonical_json,
    canonicalize_args,
    compute_action_digest,
    make_registry_entry,
    sha256_hex,
    validate_input,
    validate_output,
    validate_value,
    verify_action_digest,
)
from sovereign_claw.tools_basic import (
    GOVERNED_TOOL_REGISTRY,
    TOOL_SPEC_V1_ECHO,
    TOOL_SPEC_V1_LIST_DIR,
    TOOL_SPEC_V1_READ_FILE,
    TOOL_SPEC_V1_WRITE_JSON,
    FilesystemCapability,
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
        d: Dict[str, Any] = {}
        d["self"] = d
        with pytest.raises(CyclicValueError, match="Cyclic"):
            canonical_json(d)

    def test_list_self_reference_rejected(self):
        lst: List[Any] = []
        lst.append(lst)
        with pytest.raises(CyclicValueError, match="Cyclic"):
            canonical_json(lst)

    def test_nested_cycle_rejected(self):
        inner: Dict[str, Any] = {}
        outer: Dict[str, Any] = {"inner": inner}
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
        from sovereign_claw.tool_authority import _validate_schema_structure, _MAX_DEPTH
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
    from sovereign_claw.thermodynamics import TaskManifold

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
    # Register callable under tool_id so governed lookup succeeds
    orch.register_tool(tool_id, echo)
    return orch, registry, entry


class TestGovernedOrchestratorPreview:
    def _manifold(self) -> Any:
        from sovereign_claw.thermodynamics import TaskManifold
        return TaskManifold(objective="test", t_max_steps=3)

    def test_governed_preview_returns_authority_metadata(self):
        orch, registry, entry = _make_governed_orchestrator()
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
        orch, registry, entry = _make_governed_orchestrator()
        result = orch.preview(self._manifold())
        assert result["action_digest"] is not None
        assert len(result["action_digest"]) == 64

    def test_governed_preview_unregistered_tool_not_approvable(self):
        from sovereign_claw.orchestrator import Orchestrator
        # Empty registry, tool registered as callable only under legacy name
        registry = ToolRegistry()
        orch = Orchestrator(
            llm_backend=_EchoBackend(tool="unregistered_tool", kwargs={"text": "hi"}),
            tool_registry=registry,
        )
        orch.register_tool("unregistered_tool", lambda text: text)
        result = orch.preview(self._manifold())
        assert result["approvable"] is False
        assert result["status"] == "preview-unknown-tool"

    def test_governed_preview_action_digest_stable_across_calls(self):
        orch, registry, entry = _make_governed_orchestrator()
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
        orch.register_tool("builtin.echo_text", lambda text: text)
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
        orch1.register_tool("builtin.echo_text", lambda text: text)

        orch2 = Orchestrator(
            llm_backend=_EchoBackend(tool="builtin.echo_text", kwargs={"text": "bar"}),
            tool_registry=registry,
        )
        orch2.register_tool("builtin.echo_text", lambda text: text)

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
        orch.register_tool("builtin.echo_text", fn)
        return orch, registry, entry

    def test_governed_execute_succeeds_with_correct_digest(self):
        """With the correct approved digest the action executes (not mismatch halted)."""
        orch, registry, entry = self._governed_orch_for_echo()

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
        orch, registry, entry = self._governed_orch_for_echo(echo_fn=echo_fn)

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

        llm = _EchoBackend(
            tool="test.returns_wrong_type", kwargs={"text": "hello"}
        )
        orch = Orchestrator(llm_backend=llm, tool_registry=registry)
        orch.register_tool("test.returns_wrong_type", lambda text: 42)  # returns int!

        exec_manifold = self._manifold()  # no approved digest -> free execute
        receipt = orch.execute(exec_manifold)
        assert "OUTPUT_SCHEMA_INVALID" in receipt.halt_reason

    def test_governed_execute_output_schema_ok_does_not_fail_with_schema_error(self):
        """Output schema validation passing must not produce OUTPUT_SCHEMA_INVALID halt."""
        orch, registry, entry = self._governed_orch_for_echo()
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
        orch.register_tool("builtin.echo_text", lambda text: text)

        receipt = orch.execute(self._manifold())
        # Find authority events
        events = vault.get_evidence_records(receipt.trace_id)
        authority_events = [
            e for e in events if "authority" in e.evidence_type
        ]
        assert len(authority_events) >= 1
        # Authority event must contain tool_id and hashes, NOT raw file contents
        import json as _json
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
        orch.register_tool("builtin.echo_text", echo_fn)

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
        import json as _json
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
        result = scoped_write_json_file(
            root_id, "existing.json", {"new": "data"}, overwrite=True
        )
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
        d: Dict[str, Any] = {}
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
        import json as _json

        spec = TOOL_SPEC_V1_ECHO
        entry = make_registry_entry(spec)
        registry = ToolRegistry()
        registry.register(entry)

        vault = ProofVault()
        llm = _EchoBackend(tool="builtin.echo_text", kwargs={"text": "my secret text"})
        orch = Orchestrator(llm_backend=llm, tool_registry=registry, vault=vault)
        orch.register_tool("builtin.echo_text", lambda text: text)

        receipt = orch.execute(
            TaskManifold(objective="test", t_max_steps=5)
        )

        events = vault.get_evidence_records(receipt.trace_id)
        authority_events = [e for e in events if "authority" in e.evidence_type]
        assert len(authority_events) >= 1

        for ev in authority_events:
            payload = _json.loads(ev.canonical_payload)
            # Must have hashes
            assert "tool_contract_hash" in payload
            assert "action_digest" in payload
            # Must NOT store raw text bodies
            raw_payload_str = ev.canonical_payload
            assert "my secret text" not in raw_payload_str
