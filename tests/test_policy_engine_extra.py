from __future__ import annotations

import copy
import json
import os
import sys
import time
from pathlib import Path

import pytest

import sovereign_claw.policy_engine as policy_engine_module
from sovereign_claw.policy_engine import (
    MAX_OPA_POLICY_FILE_BYTES,
    MAX_POLICY_TEXT_BYTES,
    OpaMode,
    PolicyDecisionClass,
    PolicyEngine,
    PolicyExecutionContext,
    PolicyProfile,
)


def test_legacy_evaluate_does_not_coerce_or_trust_authority_shaped_fields():
    engine = PolicyEngine()
    request = {
        "tool": "echo_text",
        "trace_id": 123,
        "session_id": {"forged": "session"},
        "correlation_id": True,
        "drift": "0.95",
        "tool_call_count": "7",
        "tool_contract_hash": "forged-hash",
        "tool_risk_class": "critical",
        "tool_capabilities": ["admin"],
        "config_identity_hash": "cfg-forged",
        "provider_identity": "trusted",
        "fallback_identity": "trusted-fallback",
        "agent_id": "trusted-agent",
    }
    context = engine._coerce_context(request)
    assert context.trace_id == ""
    assert context.session_id == ""
    assert context.correlation_id == ""
    assert context.drift_value == 0.0
    assert context.action_count == 0
    assert context.tool_contract_hash == "legacy-unbound"
    assert context.tool_risk_class == "unknown"
    assert context.tool_capabilities == ()
    assert context.config_identity_hash == "legacy"
    assert context.provider_identity == "legacy-provider"
    assert context.model_claims["caller_tool_contract_hash"] == "forged-hash"
    assert context.model_claims["caller_tool_risk_class"] == "critical"
    assert context.model_claims["caller_tool_capabilities"] == ("admin",)
    assert context.model_claims["caller_config_identity_hash"] == "cfg-forged"
    assert context.model_claims["caller_provider_identity"] == "trusted"
    assert context.model_claims["caller_agent_id"] == "trusted-agent"


def _authoritative_engine(**kwargs) -> PolicyEngine:
    return PolicyEngine(
        rego_policy_dir=kwargs.pop("rego_policy_dir", None),
        opa_mode=kwargs.pop("opa_mode", OpaMode.AUTHORITATIVE),
        **kwargs,
    )


def _mock_opa_ok(monkeypatch, payload: dict[str, object]) -> None:
    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: sys.executable)
    monkeypatch.setattr(PolicyEngine, "_local_evaluator_identity", lambda self: "local-id")
    monkeypatch.setattr(
        PolicyEngine,
        "_snapshot_policy_dir",
        lambda self, root: policy_engine_module._PolicySnapshot(
            digest="digest", snapshot_root=root, cleanup_handle=None
        ),
    )
    monkeypatch.setattr(
        PolicyEngine,
        "_snapshot_opa_evaluator",
        lambda self: policy_engine_module._EvaluatorSnapshot(
            binary_path=Path(sys.executable), identity="evaluator-id", cleanup_handle=None
        ),
    )
    monkeypatch.setattr(
        PolicyEngine,
        "_resolve_opa_evaluator_identity",
        lambda self: (Path(sys.executable), "evaluator-id"),
    )
    monkeypatch.setattr(
        PolicyEngine,
        "_run_bounded_subprocess",
        lambda self, **kwargs: {
            "returncode": 0,
            "stdout": json.dumps(payload).encode("utf-8"),
            "stderr": b"",
            "stdout_overflow": False,
            "stderr_overflow": False,
            "timed_out": False,
        },
    )


def test_evaluate_allows_clean_request_without_opa():
    engine = PolicyEngine()
    decision = engine.evaluate({"tool": "echo_text", "payload": {"x": 1}})
    assert decision.allowed is True
    assert decision.decision_class == PolicyDecisionClass.ALLOW.value


def test_evaluate_blocks_forbidden_tool():
    engine = PolicyEngine(forbidden_tools=["shell_exec"])
    decision = engine.evaluate({"tool": "shell_exec"})
    assert decision.allowed is False
    assert "local.forbidden_tools" in decision.matched_policies


def test_evaluate_blocks_oversized_payload():
    engine = PolicyEngine(max_payload_bytes=10, profile=PolicyProfile.STRICT)
    large_payload = "x" * 20000
    decision = engine.evaluate({"tool": "echo_text", "payload": large_payload})
    assert decision.allowed is False
    assert any("exceeds limit" in reason for reason in decision.reasons)


def test_evaluate_blocks_missing_trace_id_when_required():
    engine = PolicyEngine(require_trace_id=True, profile=PolicyProfile.STRICT)
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is False
    assert "trace_id is required by policy" in decision.reasons


def test_authoritative_opa_allow_false_without_reasons_still_denies(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    _mock_opa_ok(
        monkeypatch,
        {"result": [{"expressions": [{"value": {"allow": False, "deny": [], "matched": []}}]}]},
    )
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_DENY.value


def test_authoritative_opa_allow_true_allows_when_local_allows(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    _mock_opa_ok(
        monkeypatch,
        {
            "result": [
                {"expressions": [{"value": {"allow": True, "deny": [], "matched": ["opa.allow"]}}]}
            ]
        },
    )
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is True
    assert "opa.allow" in decision.matched_policies


@pytest.mark.parametrize(
    "payload",
    [
        {"result": []},
        {"result": [{"expressions": []}]},
        {"result": [{"expressions": [{"value": True}]}]},
        {"result": [{"expressions": [{"value": {"deny": []}}]}]},
        {"result": [{"expressions": [{"value": {"allow": "yes", "deny": [], "matched": []}}]}]},
    ],
)
def test_authoritative_opa_malformed_values_fail_closed(monkeypatch, tmp_path, payload):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    _mock_opa_ok(monkeypatch, payload)
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_UNAVAILABLE.value


def test_authoritative_opa_multiple_result_entries_fail_closed(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    _mock_opa_ok(
        monkeypatch,
        {
            "result": [
                {"expressions": [{"value": {"allow": True, "deny": [], "matched": []}}]},
                {"expressions": [{"value": {"allow": False, "deny": ["x"], "matched": ["y"]}}]},
            ]
        },
    )
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_UNAVAILABLE.value
    assert "OPA_RESULT_AMBIGUOUS" in ";".join(decision.reasons)


def test_authoritative_opa_multiple_expression_entries_fail_closed(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    _mock_opa_ok(
        monkeypatch,
        {
            "result": [
                {
                    "expressions": [
                        {"value": {"allow": True, "deny": [], "matched": []}},
                        {"value": {"allow": False, "deny": ["x"], "matched": ["y"]}},
                    ]
                }
            ]
        },
    )
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_UNAVAILABLE.value
    assert "OPA_EXPRESSIONS_AMBIGUOUS" in ";".join(decision.reasons)


def test_authoritative_missing_opa_binary_fails_closed(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: None)
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_UNAVAILABLE.value


def test_advisory_mode_labels_failure_but_does_not_deny(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path, opa_mode=OpaMode.ADVISORY)
    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: None)
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is True
    assert decision.opa_status == "advisory-unavailable"
    assert any(reason.startswith("advisory:") for reason in decision.reasons)


def test_disabled_mode_is_local_only(monkeypatch, tmp_path):
    engine = PolicyEngine(rego_policy_dir=tmp_path, opa_mode=OpaMode.DISABLED)
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is True
    assert decision.opa_status == "disabled"


def test_disabled_mode_local_bundle_identity_failure_denies(monkeypatch, tmp_path):
    engine = PolicyEngine(rego_policy_dir=tmp_path, opa_mode=OpaMode.DISABLED)

    def _raise_local_identity(self):
        raise ValueError("local evaluator unavailable")

    monkeypatch.setattr(PolicyEngine, "_local_evaluator_identity", _raise_local_identity)
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_INFRA_FAILURE.value
    assert "LOCAL_POLICY_IDENTITY_ERROR" in ";".join(decision.reasons)


def test_disabled_mode_bound_policy_bundle_hash_mismatch_denies():
    engine = PolicyEngine(opa_mode=OpaMode.DISABLED)
    context = engine.build_execution_context(
        trace_id="t",
        session_id="s",
        correlation_id="c",
        principal_identity="p",
        principal_scopes=[],
        policy_profile="balanced",
        lane="default",
        drift_value=0.1,
        drift_components={"scalar": 0.1},
        requested_tool="echo_text",
        tool_id="echo_text",
        tool_contract_hash="h",
        tool_risk_class="low",
        tool_capabilities=[],
        config_identity_hash="cfg",
        runtime_identity="rt",
        provider_identity="provider",
        fallback_identity="",
        budget_state={},
        resource_state={},
        execution_intent_id="unset",
        approval_correlation_id="unset",
        remaining_deadline_ms=100,
        action_count=0,
        step_index=0,
        request_payload_bytes=1,
        model_claims={},
    )
    decision = engine.evaluate_context(context, bound_policy_bundle_hash="0" * 64)
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_INFRA_FAILURE.value
    assert "POLICY_BUNDLE_HASH_MISMATCH" in ";".join(decision.reasons)


def test_policy_infra_failures_do_not_increment_learned_denials(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: None)

    for _ in range(5):
        decision = engine.evaluate({"tool": "echo_text"})
        assert decision.allowed is False
    history = engine.get_violation_history()
    assert "echo_text" not in history


def test_policy_bundle_hash_stable_across_restart_and_changes_with_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(PolicyEngine, "_digest_policy_dir", lambda self, root: "digest")
    e1 = _authoritative_engine(rego_policy_dir=tmp_path, profile=PolicyProfile.BALANCED)
    e2 = _authoritative_engine(rego_policy_dir=tmp_path, profile=PolicyProfile.BALANCED)
    assert e1.policy_bundle_hash() == e2.policy_bundle_hash()

    e3 = _authoritative_engine(rego_policy_dir=tmp_path, profile=PolicyProfile.STRICT)
    assert e1.policy_bundle_hash() != e3.policy_bundle_hash()


def test_evaluate_rejects_authoritative_context_dictionaries():
    engine = PolicyEngine()

    authoritative_context = {
        "context_version": "1",
        "trace_id": "t1",
        "session_id": "s1",
        "correlation_id": "c1",
        "principal_identity": "p",
        "principal_scopes": [],
        "policy_profile": "balanced",
        "lane": "default",
        "drift_value": 0.1,
        "drift_components": {"scalar": 0.1},
        "requested_tool": "echo_text",
        "tool_id": "echo_text",
        "tool_contract_hash": "",
        "tool_risk_class": "low",
        "tool_capabilities": [],
        "config_identity_hash": "cfg",
        "runtime_identity": "rt",
        "provider_identity": "provider",
        "fallback_identity": "",
        "budget_state": {},
        "resource_state": {},
        "execution_intent_id": "",
        "approval_correlation_id": "",
        "remaining_deadline_ms": 0,
        "action_count": 0,
        "step_index": 0,
        "request_payload_bytes": 1,
        "model_claims": {},
    }
    with pytest.raises(
        ValueError,
        match="authoritative policy context dictionaries are not accepted by evaluate",
    ):
        engine.evaluate(copy.deepcopy(authoritative_context))


def test_non_finite_context_value_is_rejected():
    engine = PolicyEngine()
    with pytest.raises(ValueError):
        engine.build_execution_context(
            trace_id="t",
            session_id="s",
            correlation_id="c",
            principal_identity="p",
            principal_scopes=[],
            policy_profile="balanced",
            lane="default",
            drift_value=float("nan"),
            drift_components={"scalar": 0.0},
            requested_tool="echo_text",
            tool_id="echo_text",
            tool_contract_hash="",
            tool_risk_class="low",
            tool_capabilities=[],
            config_identity_hash="cfg",
            runtime_identity="rt",
            provider_identity="provider",
            fallback_identity="",
            budget_state={},
            resource_state={},
            execution_intent_id="",
            approval_correlation_id="",
            remaining_deadline_ms=0,
            action_count=0,
            step_index=0,
            request_payload_bytes=1,
        )


def test_policy_execution_context_is_deeply_immutable():
    engine = PolicyEngine()
    budget = {"limits": {"cpu_ms": 10}, "labels": ["a", "b"]}
    resource = {"quota": {"tokens": 100}}
    claims = {"caller": {"session": "s1"}, "scopes": ["scope.a"]}
    context = engine.build_execution_context(
        trace_id="t",
        session_id="s",
        correlation_id="c",
        principal_identity="p",
        principal_scopes=["scope.a"],
        policy_profile="balanced",
        lane="default",
        drift_value=0.1,
        drift_components={"scalar": 0.1},
        requested_tool="echo_text",
        tool_id="echo_text",
        tool_contract_hash="h",
        tool_risk_class="low",
        tool_capabilities=[],
        config_identity_hash="cfg",
        runtime_identity="rt",
        provider_identity="provider",
        fallback_identity="",
        budget_state=budget,
        resource_state=resource,
        execution_intent_id="unset",
        approval_correlation_id="unset",
        remaining_deadline_ms=50,
        action_count=0,
        step_index=0,
        request_payload_bytes=10,
        model_claims=claims,
    )
    baseline_hash = context.context_hash()
    budget["limits"]["cpu_ms"] = 999
    claims["scopes"].append("scope.b")
    assert context.context_hash() == baseline_hash
    with pytest.raises(TypeError):
        context.budget_state["new"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        context.model_claims["caller"]["session"] = "tampered"  # type: ignore[index]
    assert context.context_hash() == baseline_hash


def test_digest_policy_dir_does_not_use_unbounded_read_bytes(monkeypatch, tmp_path):
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    (policy_dir / "policy.rego").write_text("package sovereign_claw\n", encoding="utf-8")
    engine = _authoritative_engine(rego_policy_dir=policy_dir)

    def _boom(self):
        raise AssertionError("read_bytes must not be used for policy digesting")

    monkeypatch.setattr(Path, "read_bytes", _boom)
    digest = engine._digest_policy_dir(policy_dir)
    assert isinstance(digest, str)
    assert len(digest) == 64


def test_bundle_hash_failure_missing_policy_dir_returns_stable_denial(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path / "missing")
    monkeypatch.setattr(
        PolicyEngine,
        "_evaluate_with_opa_context",
        lambda self, context: (_ for _ in ()).throw(
            AssertionError("must not run OPA on bundle failure")
        ),
    )
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_UNAVAILABLE.value
    assert "OPA_POLICY_DIR_MISSING" in ";".join(decision.reasons)
    assert decision.policy_bundle_hash == engine.policy_bundle_hash()


def test_bundle_hash_failure_unreadable_policy_dir_returns_stable_denial(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    monkeypatch.setattr(
        PolicyEngine,
        "_snapshot_policy_dir",
        lambda self, root: (_ for _ in ()).throw(PermissionError("denied")),
    )
    monkeypatch.setattr(
        PolicyEngine,
        "_evaluate_with_opa_context",
        lambda self, context: (_ for _ in ()).throw(
            AssertionError("must not run OPA on bundle failure")
        ),
    )
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_INFRA_FAILURE.value
    assert "OPA_POLICY_DIR_UNREADABLE" in ";".join(decision.reasons)


def test_bundle_hash_failure_oversized_policy_file_returns_stable_denial(tmp_path):
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    (policy_dir / "large.rego").write_bytes(b"x" * (MAX_OPA_POLICY_FILE_BYTES + 1))
    engine = _authoritative_engine(rego_policy_dir=policy_dir)
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_INFRA_FAILURE.value
    assert any(
        reason.startswith("OPA_POLICY_DIR_INVALID:policy file exceeds max size")
        for reason in decision.reasons
    )


def test_bundle_hash_failure_raced_policy_dir_returns_stable_denial(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    monkeypatch.setattr(
        PolicyEngine,
        "_snapshot_policy_dir",
        lambda self, root: (_ for _ in ()).throw(ValueError("policy file changed during digest")),
    )
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_INFRA_FAILURE.value
    assert any(
        reason.startswith("OPA_POLICY_DIR_INVALID:policy file changed during digest")
        for reason in decision.reasons
    )


def test_unsupported_learned_signal_authoritative_mode_is_rejected():
    with pytest.raises(
        ValueError,
        match="LEARNED_SIGNAL_MODE_UNSUPPORTED: authoritative requires persisted root",
    ):
        PolicyEngine(opa_mode=OpaMode.DISABLED, learned_signal_mode="authoritative")


def test_bounded_subprocess_timeout_covers_stdin_delivery(tmp_path):
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    engine = PolicyEngine()
    start = time.monotonic()
    result = engine._run_bounded_subprocess(
        cmd=[sys.executable, str(sleeper)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "LANG": "C", "LC_ALL": "C"},
        stdin_data=b"x" * (4 * 1024 * 1024),
        timeout_ms=150,
        max_stdout=1024,
        max_stderr=1024,
    )
    elapsed = time.monotonic() - start
    assert result["timed_out"] is True
    assert elapsed < 2.0


def test_authoritative_opa_exhausted_deadline_fails_closed_without_subprocess(
    monkeypatch, tmp_path
):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    _mock_opa_ok(
        monkeypatch,
        {"result": [{"expressions": [{"value": {"allow": True, "deny": [], "matched": []}}]}]},
    )
    launched = {"called": False}

    def _should_not_run(self, **kwargs):
        launched["called"] = True
        raise AssertionError("OPA subprocess must not launch when deadline is exhausted")

    monkeypatch.setattr(PolicyEngine, "_run_bounded_subprocess", _should_not_run)
    context = engine.build_execution_context(
        trace_id="t",
        session_id="s",
        correlation_id="c",
        principal_identity="p",
        principal_scopes=[],
        policy_profile="balanced",
        lane="default",
        drift_value=0.1,
        drift_components={"scalar": 0.1},
        requested_tool="echo_text",
        tool_id="echo_text",
        tool_contract_hash="h",
        tool_risk_class="low",
        tool_capabilities=[],
        config_identity_hash="cfg",
        runtime_identity="rt",
        provider_identity="provider",
        fallback_identity="",
        budget_state={},
        resource_state={},
        execution_intent_id="unset",
        approval_correlation_id="unset",
        remaining_deadline_ms=0,
        action_count=0,
        step_index=0,
        request_payload_bytes=1,
        model_claims={},
    )
    decision = engine.evaluate_context(context)
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_UNAVAILABLE.value
    assert "OPA_DEADLINE_EXHAUSTED" in ";".join(decision.reasons)
    assert launched["called"] is False


def test_authoritative_opa_uses_remaining_deadline_when_smaller_than_config(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path, opa_timeout_ms=5000)
    _mock_opa_ok(
        monkeypatch,
        {"result": [{"expressions": [{"value": {"allow": True, "deny": [], "matched": []}}]}]},
    )
    captured = {"timeout_ms": None}

    def _run(self, **kwargs):
        captured["timeout_ms"] = kwargs["timeout_ms"]
        return {
            "returncode": 0,
            "stdout": json.dumps(
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
        }

    monkeypatch.setattr(PolicyEngine, "_run_bounded_subprocess", _run)
    context = engine.build_execution_context(
        trace_id="t",
        session_id="s",
        correlation_id="c",
        principal_identity="p",
        principal_scopes=[],
        policy_profile="balanced",
        lane="default",
        drift_value=0.1,
        drift_components={"scalar": 0.1},
        requested_tool="echo_text",
        tool_id="echo_text",
        tool_contract_hash="h",
        tool_risk_class="low",
        tool_capabilities=[],
        config_identity_hash="cfg",
        runtime_identity="rt",
        provider_identity="provider",
        fallback_identity="",
        budget_state={},
        resource_state={},
        execution_intent_id="unset",
        approval_correlation_id="unset",
        remaining_deadline_ms=1,
        action_count=0,
        step_index=0,
        request_payload_bytes=1,
        model_claims={},
    )
    decision = engine.evaluate_context(context)
    assert decision.allowed is True
    assert captured["timeout_ms"] == 1


def test_policy_snapshot_binds_opa_eval_to_hashed_bytes(monkeypatch, tmp_path):
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    policy_file = policy_dir / "policy.rego"
    policy_file.write_text("package sovereign_claw\nallow := true\n", encoding="utf-8")
    engine = _authoritative_engine(rego_policy_dir=policy_dir)

    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: sys.executable)
    monkeypatch.setattr(
        PolicyEngine,
        "_resolve_opa_evaluator_identity",
        lambda self: (Path(sys.executable), "evaluator-id"),
    )

    def _identity(self, binary):
        policy_file.write_text("package sovereign_claw\nallow := false\n", encoding="utf-8")
        return "evaluator-id"

    monkeypatch.setattr(PolicyEngine, "_opa_evaluator_identity", _identity)

    def _run(self, **kwargs):
        cmd = kwargs["cmd"]
        data_idx = cmd.index("--data") + 1
        snapshot_dir = Path(cmd[data_idx])
        assert snapshot_dir != policy_dir
        assert (snapshot_dir / "policy.rego").read_text(encoding="utf-8") == (
            "package sovereign_claw\nallow := true\n"
        )
        return {
            "returncode": 0,
            "stdout": json.dumps(
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
        }

    monkeypatch.setattr(PolicyEngine, "_run_bounded_subprocess", _run)
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is True
    assert decision.decision_class == PolicyDecisionClass.ALLOW.value


def test_opa_evaluator_snapshot_executes_hashed_bytes(monkeypatch, tmp_path):
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    (policy_dir / "policy.rego").write_text(
        "package sovereign_claw\nallow := true\n", encoding="utf-8"
    )
    real_opa = tmp_path / "opa-real"
    real_opa.write_text("#!/usr/bin/env python3\nprint('old')\n", encoding="utf-8")
    real_opa.chmod(0o700)
    engine = _authoritative_engine(rego_policy_dir=policy_dir)
    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: str(real_opa))
    monkeypatch.setattr(PolicyEngine, "_local_evaluator_identity", lambda self: "local-id")

    seen = {"cmd0": ""}

    def _run(self, **kwargs):
        seen["cmd0"] = kwargs["cmd"][0]
        assert seen["cmd0"] != str(real_opa)
        assert Path(seen["cmd0"]).read_text(encoding="utf-8").endswith("print('old')\n")
        real_opa.write_text("#!/usr/bin/env python3\nprint('new')\n", encoding="utf-8")
        return {
            "returncode": 0,
            "stdout": json.dumps(
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
        }

    monkeypatch.setattr(PolicyEngine, "_run_bounded_subprocess", _run)
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is True
    assert seen["cmd0"]


def test_digest_policy_dir_rejects_directory_symlink(tmp_path):
    target = tmp_path / "real-policy"
    target.mkdir()
    (target / "policy.rego").write_text("package sovereign_claw\n", encoding="utf-8")
    symlink_root = tmp_path / "policy-link"
    symlink_root.symlink_to(target, target_is_directory=True)
    engine = _authoritative_engine(rego_policy_dir=symlink_root)
    with pytest.raises(ValueError, match="root is symlink"):
        engine._digest_policy_dir(symlink_root)


def test_digest_policy_dir_rejects_excess_empty_directories(tmp_path):
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    for idx in range(0, 2100):
        (policy_dir / f"d{idx}").mkdir()
    engine = _authoritative_engine(rego_policy_dir=policy_dir)
    with pytest.raises(ValueError, match="entry cap|directory cap"):
        engine._digest_policy_dir(policy_dir)


def test_digest_policy_dir_rejects_excessive_depth(tmp_path):
    policy_dir = tmp_path / "policy"
    policy_dir.mkdir()
    current = policy_dir
    for idx in range(0, 40):
        current = current / f"d{idx}"
        current.mkdir()
    (current / "policy.rego").write_text("package sovereign_claw\n", encoding="utf-8")
    engine = _authoritative_engine(rego_policy_dir=policy_dir)
    with pytest.raises(ValueError, match="depth cap"):
        engine._digest_policy_dir(policy_dir)


def test_policy_bundle_hash_changes_when_evaluator_identity_changes(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    monkeypatch.setattr(PolicyEngine, "_digest_policy_dir", lambda self, root: "digest")
    monkeypatch.setattr(PolicyEngine, "_local_evaluator_identity", lambda self: "local-id")
    monkeypatch.setattr(
        PolicyEngine,
        "_resolve_opa_evaluator_identity",
        lambda self: (Path(sys.executable), "evaluator-A"),
    )
    hash_a = engine.policy_bundle_hash()
    monkeypatch.setattr(
        PolicyEngine,
        "_resolve_opa_evaluator_identity",
        lambda self: (Path(sys.executable), "evaluator-B"),
    )
    hash_b = engine.policy_bundle_hash()
    assert hash_a != hash_b


def test_policy_bundle_hash_changes_when_local_evaluator_identity_changes(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path, opa_mode=OpaMode.DISABLED)
    monkeypatch.setattr(PolicyEngine, "_local_evaluator_identity", lambda self: "local-A")
    hash_a = engine.policy_bundle_hash()
    monkeypatch.setattr(PolicyEngine, "_local_evaluator_identity", lambda self: "local-B")
    hash_b = engine.policy_bundle_hash()
    assert hash_a != hash_b


def test_disabled_mode_stale_profile_or_evaluator_bound_hash_denies(monkeypatch):
    engine = PolicyEngine(opa_mode=OpaMode.DISABLED)
    monkeypatch.setattr(PolicyEngine, "_local_evaluator_identity", lambda self: "local-A")
    stale_hash = engine.policy_bundle_hash("balanced")
    monkeypatch.setattr(PolicyEngine, "_local_evaluator_identity", lambda self: "local-B")
    context = engine.build_execution_context(
        trace_id="t",
        session_id="s",
        correlation_id="c",
        principal_identity="p",
        principal_scopes=[],
        policy_profile="strict",
        lane="default",
        drift_value=0.1,
        drift_components={"scalar": 0.1},
        requested_tool="echo_text",
        tool_id="echo_text",
        tool_contract_hash="h",
        tool_risk_class="low",
        tool_capabilities=[],
        config_identity_hash="cfg",
        runtime_identity="rt",
        provider_identity="provider",
        fallback_identity="",
        budget_state={},
        resource_state={},
        execution_intent_id="unset",
        approval_correlation_id="unset",
        remaining_deadline_ms=100,
        action_count=0,
        step_index=0,
        request_payload_bytes=1,
        model_claims={},
    )
    decision = engine.evaluate_context(context, bound_policy_bundle_hash=stale_hash)
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_INFRA_FAILURE.value
    assert "POLICY_BUNDLE_HASH_MISMATCH" in ";".join(decision.reasons)


def test_policy_bundle_hash_changes_when_profile_defaults_change(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path, opa_mode=OpaMode.DISABLED)
    monkeypatch.setattr(PolicyEngine, "_local_evaluator_identity", lambda self: "local-id")
    hash_a = engine.policy_bundle_hash("balanced")
    patched = copy.deepcopy(policy_engine_module.PROFILE_DEFAULTS)
    patched[PolicyProfile.BALANCED]["drift_threshold"] = 0.123
    monkeypatch.setattr(policy_engine_module, "PROFILE_DEFAULTS", patched)
    hash_b = engine.policy_bundle_hash("balanced")
    assert hash_a != hash_b


def test_policy_bundle_hash_changes_when_opa_runner_limits_change(monkeypatch, tmp_path):
    engine_a = _authoritative_engine(
        rego_policy_dir=tmp_path,
        opa_timeout_ms=500,
        opa_max_input_bytes=8192,
        opa_max_stdout_bytes=4096,
        opa_max_stderr_bytes=1024,
    )
    engine_b = _authoritative_engine(
        rego_policy_dir=tmp_path,
        opa_timeout_ms=750,
        opa_max_input_bytes=8192,
        opa_max_stdout_bytes=4096,
        opa_max_stderr_bytes=1024,
    )
    monkeypatch.setattr(PolicyEngine, "_digest_policy_dir", lambda self, root: "digest")
    monkeypatch.setattr(PolicyEngine, "_local_evaluator_identity", lambda self: "local-id")
    monkeypatch.setattr(
        PolicyEngine,
        "_resolve_opa_evaluator_identity",
        lambda self: (Path(sys.executable), "evaluator-id"),
    )
    assert engine_a.policy_bundle_hash() != engine_b.policy_bundle_hash()


def test_policy_execution_context_direct_construction_rejects_non_string_mapping_keys():
    with pytest.raises(ValueError, match="mapping keys must be strings"):
        PolicyExecutionContext(
            context_version="1",
            trace_id="t",
            session_id="s",
            correlation_id="c",
            principal_identity="p",
            principal_scopes=(),
            policy_profile="balanced",
            lane="default",
            drift_value=0.1,
            drift_components={"scalar": 0.1},
            requested_tool="echo",
            tool_id="echo",
            tool_contract_hash="h",
            tool_risk_class="low",
            tool_capabilities=(),
            config_identity_hash="cfg",
            runtime_identity="rt",
            provider_identity="provider",
            fallback_identity="fallback",
            budget_state={1: "x"},  # type: ignore[dict-item]
            resource_state={},
            execution_intent_id="unset",
            approval_correlation_id="unset",
            remaining_deadline_ms=1,
            action_count=0,
            step_index=0,
            request_payload_bytes=1,
            model_claims={},
        )


def test_policy_execution_context_direct_construction_rejects_non_finite_values():
    with pytest.raises(ValueError, match="drift_value must be finite"):
        PolicyExecutionContext(
            context_version="1",
            trace_id="t",
            session_id="s",
            correlation_id="c",
            principal_identity="p",
            principal_scopes=(),
            policy_profile="balanced",
            lane="default",
            drift_value=float("nan"),
            drift_components={"scalar": 0.1},
            requested_tool="echo",
            tool_id="echo",
            tool_contract_hash="h",
            tool_risk_class="low",
            tool_capabilities=(),
            config_identity_hash="cfg",
            runtime_identity="rt",
            provider_identity="provider",
            fallback_identity="fallback",
            budget_state={},
            resource_state={},
            execution_intent_id="unset",
            approval_correlation_id="unset",
            remaining_deadline_ms=1,
            action_count=0,
            step_index=0,
            request_payload_bytes=1,
            model_claims={},
        )


def test_policy_execution_context_direct_construction_rejects_non_numeric_drift_values():
    with pytest.raises(ValueError, match="drift_value must be a finite real number"):
        PolicyExecutionContext(
            context_version="1",
            trace_id="t",
            session_id="s",
            correlation_id="c",
            principal_identity="p",
            principal_scopes=(),
            policy_profile="balanced",
            lane="default",
            drift_value="0.1",  # type: ignore[arg-type]
            drift_components={"scalar": 0.1},
            requested_tool="echo",
            tool_id="echo",
            tool_contract_hash="h",
            tool_risk_class="low",
            tool_capabilities=(),
            config_identity_hash="cfg",
            runtime_identity="rt",
            provider_identity="provider",
            fallback_identity="fallback",
            budget_state={},
            resource_state={},
            execution_intent_id="unset",
            approval_correlation_id="unset",
            remaining_deadline_ms=1,
            action_count=0,
            step_index=0,
            request_payload_bytes=1,
            model_claims={},
        )


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("remaining_deadline_ms", "1"),
        ("remaining_deadline_ms", 1.5),
        ("remaining_deadline_ms", True),
        ("action_count", "1"),
        ("action_count", 1.5),
        ("action_count", True),
        ("step_index", "1"),
        ("step_index", 1.5),
        ("step_index", True),
        ("request_payload_bytes", "1"),
        ("request_payload_bytes", 1.5),
        ("request_payload_bytes", True),
    ],
)
def test_policy_execution_context_direct_construction_rejects_non_integer_counters(
    field_name, field_value
):
    kwargs = dict(
        context_version="1",
        trace_id="t",
        session_id="s",
        correlation_id="c",
        principal_identity="p",
        principal_scopes=(),
        policy_profile="balanced",
        lane="default",
        drift_value=0.1,
        drift_components={"scalar": 0.1},
        requested_tool="echo",
        tool_id="echo",
        tool_contract_hash="h",
        tool_risk_class="low",
        tool_capabilities=(),
        config_identity_hash="cfg",
        runtime_identity="rt",
        provider_identity="provider",
        fallback_identity="fallback",
        budget_state={},
        resource_state={},
        execution_intent_id="unset",
        approval_correlation_id="unset",
        remaining_deadline_ms=1,
        action_count=0,
        step_index=0,
        request_payload_bytes=1,
        model_claims={},
    )
    kwargs[field_name] = field_value
    with pytest.raises(ValueError, match=f"{field_name} must be an integer"):
        PolicyExecutionContext(**kwargs)  # type: ignore[arg-type]


def test_policy_execution_context_direct_construction_rejects_cycles():
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="contains cycle"):
        PolicyExecutionContext(
            context_version="1",
            trace_id="t",
            session_id="s",
            correlation_id="c",
            principal_identity="p",
            principal_scopes=(),
            policy_profile="balanced",
            lane="default",
            drift_value=0.1,
            drift_components={"scalar": 0.1},
            requested_tool="echo",
            tool_id="echo",
            tool_contract_hash="h",
            tool_risk_class="low",
            tool_capabilities=(),
            config_identity_hash="cfg",
            runtime_identity="rt",
            provider_identity="provider",
            fallback_identity="fallback",
            budget_state={"budget": cyclic},
            resource_state={},
            execution_intent_id="unset",
            approval_correlation_id="unset",
            remaining_deadline_ms=1,
            action_count=0,
            step_index=0,
            request_payload_bytes=1,
            model_claims={},
        )


def test_policy_execution_context_direct_construction_rejects_non_finite_nested_numbers():
    with pytest.raises(ValueError, match="requires finite numbers"):
        PolicyExecutionContext(
            context_version="1",
            trace_id="t",
            session_id="s",
            correlation_id="c",
            principal_identity="p",
            principal_scopes=(),
            policy_profile="balanced",
            lane="default",
            drift_value=0.1,
            drift_components={"scalar": 0.1},
            requested_tool="echo",
            tool_id="echo",
            tool_contract_hash="h",
            tool_risk_class="low",
            tool_capabilities=(),
            config_identity_hash="cfg",
            runtime_identity="rt",
            provider_identity="provider",
            fallback_identity="fallback",
            budget_state={},
            resource_state={},
            execution_intent_id="unset",
            approval_correlation_id="unset",
            remaining_deadline_ms=1,
            action_count=0,
            step_index=0,
            request_payload_bytes=1,
            model_claims={"score": float("inf")},
        )


def test_build_execution_context_rejects_overlong_authority_identifiers():
    engine = PolicyEngine()
    overlong_a = "a" * (MAX_POLICY_TEXT_BYTES + 1)
    with pytest.raises(ValueError, match="principal_identity exceeds"):
        engine.build_execution_context(
            trace_id="t",
            session_id="s",
            correlation_id="c",
            principal_identity=overlong_a,
            principal_scopes=[],
            policy_profile="balanced",
            lane="default",
            drift_value=0.1,
            drift_components={"scalar": 0.1},
            requested_tool="echo_text",
            tool_id="echo_text",
            tool_contract_hash="h",
            tool_risk_class="low",
            tool_capabilities=[],
            config_identity_hash="cfg",
            runtime_identity="rt",
            provider_identity="provider",
            fallback_identity="",
            budget_state={},
            resource_state={},
            execution_intent_id="unset",
            approval_correlation_id="unset",
            remaining_deadline_ms=10,
            action_count=0,
            step_index=0,
            request_payload_bytes=1,
            model_claims={},
        )


def test_build_execution_context_rejects_overlong_authority_scope_and_capability():
    engine = PolicyEngine()
    overlong = "s" * (MAX_POLICY_TEXT_BYTES + 1)
    with pytest.raises(ValueError, match="principal_scopes value exceeds"):
        engine.build_execution_context(
            trace_id="t",
            session_id="s",
            correlation_id="c",
            principal_identity="p",
            principal_scopes=[overlong],
            policy_profile="balanced",
            lane="default",
            drift_value=0.1,
            drift_components={"scalar": 0.1},
            requested_tool="echo_text",
            tool_id="echo_text",
            tool_contract_hash="h",
            tool_risk_class="low",
            tool_capabilities=[],
            config_identity_hash="cfg",
            runtime_identity="rt",
            provider_identity="provider",
            fallback_identity="",
            budget_state={},
            resource_state={},
            execution_intent_id="unset",
            approval_correlation_id="unset",
            remaining_deadline_ms=10,
            action_count=0,
            step_index=0,
            request_payload_bytes=1,
            model_claims={},
        )
    with pytest.raises(ValueError, match="tool_capabilities value exceeds"):
        engine.build_execution_context(
            trace_id="t",
            session_id="s",
            correlation_id="c",
            principal_identity="p",
            principal_scopes=[],
            policy_profile="balanced",
            lane="default",
            drift_value=0.1,
            drift_components={"scalar": 0.1},
            requested_tool="echo_text",
            tool_id="echo_text",
            tool_contract_hash="h",
            tool_risk_class="low",
            tool_capabilities=[overlong],
            config_identity_hash="cfg",
            runtime_identity="rt",
            provider_identity="provider",
            fallback_identity="",
            budget_state={},
            resource_state={},
            execution_intent_id="unset",
            approval_correlation_id="unset",
            remaining_deadline_ms=10,
            action_count=0,
            step_index=0,
            request_payload_bytes=1,
            model_claims={},
        )
