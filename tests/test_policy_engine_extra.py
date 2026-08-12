from __future__ import annotations

import copy
import json

import pytest

from sovereign_claw.policy_engine import OpaMode, PolicyDecisionClass, PolicyEngine, PolicyProfile


def _authoritative_engine(**kwargs) -> PolicyEngine:
    return PolicyEngine(
        rego_policy_dir=kwargs.pop("rego_policy_dir", None),
        opa_mode=kwargs.pop("opa_mode", OpaMode.AUTHORITATIVE),
        **kwargs,
    )


def _mock_opa_ok(monkeypatch, payload: dict[str, object]) -> None:
    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: "/usr/bin/opa")
    monkeypatch.setattr(
        PolicyEngine,
        "_digest_policy_dir",
        lambda self, root: "digest",
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


def test_authoritative_missing_opa_binary_fails_closed(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: None)
    monkeypatch.setattr(PolicyEngine, "_digest_policy_dir", lambda self, root: "digest")
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is False
    assert decision.decision_class == PolicyDecisionClass.POLICY_UNAVAILABLE.value


def test_advisory_mode_labels_failure_but_does_not_deny(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path, opa_mode=OpaMode.ADVISORY)
    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: None)
    monkeypatch.setattr(PolicyEngine, "_digest_policy_dir", lambda self, root: "digest")
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is True
    assert decision.opa_status == "advisory-unavailable"
    assert any(reason.startswith("advisory:") for reason in decision.reasons)


def test_disabled_mode_is_local_only(monkeypatch, tmp_path):
    engine = PolicyEngine(rego_policy_dir=tmp_path, opa_mode=OpaMode.DISABLED)
    decision = engine.evaluate({"tool": "echo_text"})
    assert decision.allowed is True
    assert decision.opa_status == "disabled"


def test_policy_infra_failures_do_not_increment_learned_denials(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: None)
    monkeypatch.setattr(PolicyEngine, "_digest_policy_dir", lambda self, root: "digest")

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


def test_update_drift_does_not_override_explicit_context_drift(monkeypatch):
    engine = PolicyEngine()
    engine.update_drift(0.99)

    context = {
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
    decision = engine.evaluate(copy.deepcopy(context))
    assert decision.drift_at_evaluation == 0.1


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
