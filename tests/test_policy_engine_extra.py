from __future__ import annotations

import copy
import json
import os
import sys
import time
from pathlib import Path

import pytest

from sovereign_claw.policy_engine import (
    MAX_OPA_POLICY_FILE_BYTES,
    OpaMode,
    PolicyDecisionClass,
    PolicyEngine,
    PolicyProfile,
)


def _authoritative_engine(**kwargs) -> PolicyEngine:
    return PolicyEngine(
        rego_policy_dir=kwargs.pop("rego_policy_dir", None),
        opa_mode=kwargs.pop("opa_mode", OpaMode.AUTHORITATIVE),
        **kwargs,
    )


def _mock_opa_ok(monkeypatch, payload: dict[str, object]) -> None:
    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda _: sys.executable)
    monkeypatch.setattr(
        PolicyEngine,
        "_resolve_opa_evaluator_identity",
        lambda self: (Path(sys.executable), "evaluator-id"),
    )
    monkeypatch.setattr(
        PolicyEngine, "_opa_evaluator_identity", lambda self, binary: "evaluator-id"
    )
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


def test_process_local_learned_denials_are_not_authoritative_with_none_root():
    engine = PolicyEngine(opa_mode=OpaMode.DISABLED, learned_signal_mode="authoritative")
    for _ in range(3):
        engine._record_violation("echo_text", "violation")
    decision_with_state = engine.evaluate({"tool": "echo_text"})

    restarted = PolicyEngine(opa_mode=OpaMode.DISABLED, learned_signal_mode="authoritative")
    decision_after_restart = restarted.evaluate({"tool": "echo_text"})

    assert engine.learned_signal_mode == "advisory"
    assert restarted.learned_signal_mode == "advisory"
    assert decision_with_state.allowed is True
    assert decision_after_restart.allowed is True
    assert decision_with_state.decision_class == decision_after_restart.decision_class


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


def test_digest_policy_dir_rejects_directory_symlink(tmp_path):
    target = tmp_path / "real-policy"
    target.mkdir()
    (target / "policy.rego").write_text("package sovereign_claw\n", encoding="utf-8")
    symlink_root = tmp_path / "policy-link"
    symlink_root.symlink_to(target, target_is_directory=True)
    engine = _authoritative_engine(rego_policy_dir=symlink_root)
    with pytest.raises(ValueError, match="root is symlink"):
        engine._digest_policy_dir(symlink_root)


def test_policy_bundle_hash_changes_when_evaluator_identity_changes(monkeypatch, tmp_path):
    engine = _authoritative_engine(rego_policy_dir=tmp_path)
    monkeypatch.setattr(PolicyEngine, "_digest_policy_dir", lambda self, root: "digest")
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
