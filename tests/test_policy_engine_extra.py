from __future__ import annotations

import json

from sovereign_claw.policy_engine import PolicyDecision, PolicyEngine


def test_evaluate_allows_clean_request_without_opa():
    engine = PolicyEngine()

    decision = engine.evaluate({"tool": "echo_text", "payload": {"x": 1}})

    assert decision.allowed is True
    assert decision.reasons == []
    assert decision.matched_policies == []


def test_evaluate_blocks_forbidden_tool():
    engine = PolicyEngine(forbidden_tools=["shell_exec"])

    decision = engine.evaluate({"tool": "shell_exec"})

    assert decision.allowed is False
    assert "forbidden" in decision.reasons[0]
    assert "local.forbidden_tools" in decision.matched_policies


def test_evaluate_blocks_oversized_payload():
    from sovereign_claw.policy_engine import PolicyProfile

    engine = PolicyEngine(max_payload_bytes=10, profile=PolicyProfile.STRICT)
    # STRICT profile has max_payload_bytes=16384, so use a very large payload
    large_payload = "x" * 20000
    decision = engine.evaluate({"tool": "echo_text", "payload": large_payload})

    assert decision.allowed is False
    assert any("exceeds limit" in reason for reason in decision.reasons)
    assert "local.max_payload_bytes" in decision.matched_policies


def test_evaluate_blocks_missing_trace_id_when_required():
    from sovereign_claw.policy_engine import PolicyProfile

    # STRICT profile has require_trace_id=True
    engine = PolicyEngine(require_trace_id=True, profile=PolicyProfile.STRICT)

    decision = engine.evaluate({"tool": "echo_text"})

    assert decision.allowed is False
    assert "trace_id is required by policy" in decision.reasons
    assert "local.require_trace_id" in decision.matched_policies


def test_evaluate_accepts_trace_id_when_required():
    engine = PolicyEngine(require_trace_id=True)

    decision = engine.evaluate({"tool": "echo_text", "trace_id": "trace-1"})

    assert decision.allowed is True
    assert decision.reasons == []


def test_evaluate_combines_local_and_opa_decisions(monkeypatch):
    engine = PolicyEngine(forbidden_tools=["shell_exec"])

    monkeypatch.setattr(
        PolicyEngine,
        "_evaluate_with_opa",
        lambda self, request: PolicyDecision(
            allowed=False,
            reasons=["denied by opa"],
            matched_policies=["opa.test"],
        ),
    )

    decision = engine.evaluate({"tool": "shell_exec"})

    assert decision.allowed is False
    assert "tool 'shell_exec' is forbidden by local policy" in decision.reasons
    assert "denied by opa" in decision.reasons
    assert "local.forbidden_tools" in decision.matched_policies
    assert "opa.test" in decision.matched_policies


def test_evaluate_with_opa_returns_none_when_policy_dir_missing():
    engine = PolicyEngine(rego_policy_dir=None)

    assert engine._evaluate_with_opa({"tool": "echo_text"}) is None


def test_evaluate_with_opa_returns_none_when_opa_binary_missing(monkeypatch, tmp_path):
    engine = PolicyEngine(rego_policy_dir=tmp_path)

    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda name: None)

    assert engine._evaluate_with_opa({"tool": "echo_text"}) is None


def test_evaluate_with_opa_returns_runtime_error_decision_on_nonzero_exit(monkeypatch, tmp_path):
    engine = PolicyEngine(rego_policy_dir=tmp_path)

    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda name: "/usr/bin/opa")

    class DummyProc:
        returncode = 1
        stdout = b""
        stderr = b"rego exploded"

    def fake_run(cmd, input, stdout, stderr, check):
        assert cmd[0] == "/usr/bin/opa"
        assert json.loads(input.decode("utf-8")) == {"tool": "echo_text"}
        return DummyProc()

    monkeypatch.setattr("sovereign_claw.policy_engine.subprocess.run", fake_run)

    decision = engine._evaluate_with_opa({"tool": "echo_text"})

    assert decision is not None
    assert decision.allowed is False
    assert decision.reasons == ["opa evaluation failed: rego exploded"]
    assert decision.matched_policies == ["opa.runtime_error"]


def test_evaluate_with_opa_returns_none_when_no_results(monkeypatch, tmp_path):
    engine = PolicyEngine(rego_policy_dir=tmp_path)

    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda name: "/usr/bin/opa")

    class DummyProc:
        returncode = 0
        stdout = json.dumps({"result": []}).encode("utf-8")
        stderr = b""

    monkeypatch.setattr(
        "sovereign_claw.policy_engine.subprocess.run",
        lambda *args, **kwargs: DummyProc(),
    )

    decision = engine._evaluate_with_opa({"tool": "echo_text"})

    assert decision is None


def test_evaluate_with_opa_returns_none_when_no_expressions(monkeypatch, tmp_path):
    engine = PolicyEngine(rego_policy_dir=tmp_path)

    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda name: "/usr/bin/opa")

    class DummyProc:
        returncode = 0
        stdout = json.dumps({"result": [{"expressions": []}]}).encode("utf-8")
        stderr = b""

    monkeypatch.setattr(
        "sovereign_claw.policy_engine.subprocess.run",
        lambda *args, **kwargs: DummyProc(),
    )

    decision = engine._evaluate_with_opa({"tool": "echo_text"})

    assert decision is None


def test_evaluate_with_opa_returns_allowing_policy_decision(monkeypatch, tmp_path):
    engine = PolicyEngine(rego_policy_dir=tmp_path)

    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda name: "/usr/bin/opa")

    class DummyProc:
        returncode = 0
        stdout = json.dumps(
            {
                "result": [
                    {
                        "expressions": [
                            {
                                "value": {
                                    "allow": True,
                                    "deny": [],
                                    "matched": ["rego.allow"],
                                }
                            }
                        ]
                    }
                ]
            }
        ).encode("utf-8")
        stderr = b""

    monkeypatch.setattr(
        "sovereign_claw.policy_engine.subprocess.run",
        lambda *args, **kwargs: DummyProc(),
    )

    decision = engine._evaluate_with_opa({"tool": "echo_text"})

    assert decision is not None
    assert decision.allowed is True
    assert decision.reasons == []
    assert decision.matched_policies == ["rego.allow"]


def test_evaluate_with_opa_returns_denying_policy_decision(monkeypatch, tmp_path):
    engine = PolicyEngine(rego_policy_dir=tmp_path)

    monkeypatch.setattr("sovereign_claw.policy_engine.shutil.which", lambda name: "/usr/bin/opa")

    class DummyProc:
        returncode = 0
        stdout = json.dumps(
            {
                "result": [
                    {
                        "expressions": [
                            {
                                "value": {
                                    "allow": False,
                                    "deny": ["blocked by rego"],
                                    "matched": ["rego.deny"],
                                }
                            }
                        ]
                    }
                ]
            }
        ).encode("utf-8")
        stderr = b""

    monkeypatch.setattr(
        "sovereign_claw.policy_engine.subprocess.run",
        lambda *args, **kwargs: DummyProc(),
    )

    decision = engine._evaluate_with_opa({"tool": "echo_text"})

    assert decision is not None
    assert decision.allowed is False
    assert decision.reasons == ["blocked by rego"]
    assert decision.matched_policies == ["rego.deny"]
