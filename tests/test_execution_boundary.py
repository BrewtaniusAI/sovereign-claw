from __future__ import annotations

from dataclasses import replace

import pytest

from sovereign_claw.execution_boundary import (
    WORKER_SCHEMA_VERSION,
    WorkerProtocolError,
    WorkerRequestV1,
    WorkerResponseV1,
    run_subprocess_bounded_v1,
    validate_worker_response_authority,
)
from sovereign_claw.orchestrator import Orchestrator
from sovereign_claw.thermodynamics import TaskManifold
from sovereign_claw.tool_authority import ToolRegistry, make_registry_entry
from sovereign_claw.tools_basic import TOOL_SPEC_V1_ECHO


class _OnceToolBackend:
    def __init__(self, tool: str, kwargs: dict[str, object]) -> None:
        self._tool = tool
        self._kwargs = kwargs
        self._calls = 0

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        self._calls += 1
        if self._calls == 1:
            return {"tool": self._tool, "kwargs": self._kwargs, "comment": "run"}
        return {"tool": "HALT", "kwargs": {}, "comment": "done"}


def _request(**overrides: object) -> WorkerRequestV1:
    base = WorkerRequestV1(
        schema_version=WORKER_SCHEMA_VERSION,
        request_id="req-1",
        trace_id="trace-1",
        correlation_id="corr-1",
        tool_id="builtin.echo_text",
        tool_contract_hash="h" * 64,
        registry_snapshot_hash="r" * 64,
        worker_handler_id="builtin.echo_text.in_process",
        worker_build_identity="IN_PROCESS",
        isolation_profile="subprocess_bounded_v1",
        action_digest="a" * 64,
        policy_identity="p" * 64,
        principal_identity="i" * 64,
        principal_scopes=(),
        capabilities=(),
        args={"text": "hi"},
        deadline_ms=1000,
        cpu_budget_ms=None,
        memory_bytes=None,
        max_processes=None,
        max_request_bytes=256 * 1024,
        max_response_bytes=256 * 1024,
        max_stdout_bytes=64 * 1024,
        max_stderr_bytes=64 * 1024,
        max_output_bytes=64 * 1024,
        postcondition_validator_id="__none__",
        postcondition_validator_version="__none__",
        evidence_policy="digest_only",
        redaction_policy="default",
    )
    if not overrides:
        return base
    return replace(base, **overrides)


def test_worker_request_canonical_serialization_is_deterministic() -> None:
    req_a = _request(args={"z": [2, 1], "a": {"k": "v"}})
    req_b = _request(args={"a": {"k": "v"}, "z": [2, 1]})
    assert req_a.canonical_bytes() == req_b.canonical_bytes()


def test_worker_request_rejects_non_finite_json_value() -> None:
    bad = _request(args={"score": float("nan")})
    with pytest.raises(ValueError):
        _ = bad.canonical_bytes()


def test_subprocess_worker_unknown_handler_fails_closed() -> None:
    req = _request(worker_handler_id="unknown.handler")
    resp = run_subprocess_bounded_v1(req)
    assert resp.status == "PROTOCOL_ERROR"
    assert resp.request_id == req.request_id


def test_worker_response_identity_mismatch_rejected() -> None:
    spec = TOOL_SPEC_V1_ECHO
    entry = make_registry_entry(spec)
    req = _request(
        tool_id=spec.tool_id,
        tool_contract_hash=entry.tool_contract_hash,
        worker_handler_id=entry.worker_handler_id,
    )
    resp = WorkerResponseV1.from_request(req, status="SUCCEEDED", result="ok")
    tampered = replace(resp, request_id="other")
    with pytest.raises(WorkerProtocolError):
        validate_worker_response_authority(req, tampered, entry)


def test_governed_nontrusted_profile_does_not_fallback_to_in_process() -> None:
    spec = replace(
        TOOL_SPEC_V1_ECHO,
        isolation_profile="sandbox",
        worker_handler_id="builtin.echo_text.sandbox",
        worker_build_identity="SANDBOX_BUILD",
    )
    entry = make_registry_entry(spec, trusted_execution_class=None)
    registry = ToolRegistry()
    registry.register(entry)
    call_count = {"n": 0}

    def _handler(text: str) -> str:
        call_count["n"] += 1
        return text

    orch = Orchestrator(
        llm_backend=_OnceToolBackend(tool=spec.tool_id, kwargs={"text": "hello"}),
        tool_registry=registry,
    )
    orch.register_governed_handler(entry.worker_handler_id, _handler)

    preview = orch.preview(TaskManifold(objective="demo", t_max_steps=2))
    receipt = orch.execute(
        TaskManifold(
            objective="demo",
            t_max_steps=2,
            metadata={"approved_action_digest": preview["action_digest"]},
        )
    )
    assert call_count["n"] == 0
    assert orch.shield.execution_log() == []
    assert receipt.halt_reason is not None
