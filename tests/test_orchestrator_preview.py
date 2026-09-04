from sovereign_claw.cli import DemoBackend
from sovereign_claw.orchestrator import Orchestrator
from sovereign_claw.proof_vault import ProofVault
from sovereign_claw.runtime import SovereignRuntime
from sovereign_claw.thermodynamics import TaskManifold


class MalformedPreviewBackend:
    def decide_next_action(self, objective, history, forbidden_actions, drift):
        return {"tool": "", "kwargs": [], "comment": "bad"}


class EchoPreviewBackend:
    def decide_next_action(self, objective, history, forbidden_actions, drift):
        return {
            "tool": "echo_text",
            "kwargs": {"text": objective},
            "comment": "preview echo",
            "agent_id": "preview-backend",
        }


class UnknownToolBackend:
    def decide_next_action(self, objective, history, forbidden_actions, drift):
        return {
            "tool": "missing_tool",
            "kwargs": {},
            "comment": "preview unknown",
        }


class InvalidKwargsBackend:
    def decide_next_action(self, objective, history, forbidden_actions, drift):
        return {
            "tool": "echo_text",
            "kwargs": {"unexpected": "value"},
            "comment": "invalid schema",
        }


class NonFinitePreviewBackend:
    def __init__(self, value):
        self.value = value

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        return {
            "tool": "record_score",
            "kwargs": {"score": self.value},
            "comment": "invalid number",
        }


class CollidingKeysBackend:
    def decide_next_action(self, objective, history, forbidden_actions, drift):
        return {
            "tool": "echo_payload",
            "kwargs": {" key ": "left", "key": "right"},
            "comment": "colliding keys",
        }


class OverlongKeyBackend:
    def decide_next_action(self, objective, history, forbidden_actions, drift):
        return {
            "tool": "echo_payload",
            "kwargs": {"k" * 65: "value"},
            "comment": "long key",
        }


class DivergentActionBackend:
    def __init__(self, preview_action, run_action):
        self.preview_action = preview_action
        self.run_action = run_action
        self.calls = 0

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        self.calls += 1
        return self.preview_action if self.calls == 1 else self.run_action


class ApprovalScopeBackend:
    def __init__(self, approved_action, later_action):
        self.approved_action = approved_action
        self.later_action = later_action
        self.calls = 0

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        self.calls += 1
        if self.calls <= 2:
            return self.approved_action
        return self.later_action


def make_orchestrator(tmp_path, backend):
    return Orchestrator(
        llm_backend=backend,
        vault=ProofVault(db_path=tmp_path / "preview-vault.sqlite3"),
    )


def test_runtime_preview_uses_native_preview_without_tool_execution(tmp_path):
    tool_calls = {"count": 0}

    def echo_text(text: str) -> str:
        tool_calls["count"] += 1
        return text

    orchestrator = make_orchestrator(tmp_path, DemoBackend())
    orchestrator.register_tool("echo_text", echo_text)
    runtime = SovereignRuntime(orchestrator=orchestrator)

    result = runtime.preview("demo objective")

    assert result["supported"] is True
    assert result["status"] == "preview"
    assert result["action"]["tool"] == "echo_text"
    assert result["tool_calls"] == 0
    assert result["action_digest"]
    assert tool_calls["count"] == 0
    assert orchestrator.shield.execution_log() == []


def test_preview_refuses_malformed_model_output(tmp_path):
    orchestrator = make_orchestrator(tmp_path, MalformedPreviewBackend())

    result = orchestrator.preview(TaskManifold(objective="demo objective"))

    assert result["supported"] is False
    assert result["approvable"] is False
    assert result["status"] == "preview-malformed"
    assert result["tool_calls"] == 0


def test_preview_refuses_forbidden_tool_without_execution(tmp_path):
    tool_calls = {"count": 0}

    def echo_text(text: str) -> str:
        tool_calls["count"] += 1
        return text

    orchestrator = make_orchestrator(tmp_path, EchoPreviewBackend())
    orchestrator.register_tool("echo_text", echo_text)

    result = orchestrator.preview(
        TaskManifold(objective="demo objective", forbidden_actions=["echo_text"])
    )

    assert result["supported"] is True
    assert result["approvable"] is False
    assert result["status"] == "preview-forbidden"
    assert result["tool_calls"] == 0
    assert tool_calls["count"] == 0


def test_preview_refuses_unknown_tool(tmp_path):
    orchestrator = make_orchestrator(tmp_path, UnknownToolBackend())

    result = orchestrator.preview(TaskManifold(objective="demo objective"))

    assert result["supported"] is True
    assert result["approvable"] is False
    assert result["status"] == "preview-unknown-tool"
    assert result["tool_calls"] == 0


def test_preview_refuses_tool_kwargs_that_do_not_match_schema(tmp_path):
    orchestrator = make_orchestrator(tmp_path, InvalidKwargsBackend())
    orchestrator.register_tool("echo_text", lambda text: text)

    result = orchestrator.preview(TaskManifold(objective="demo objective"))

    assert result["supported"] is True
    assert result["approvable"] is False
    assert result["status"] == "preview-malformed"
    assert "tool schema" in result["expected_halt_reason"]
    assert result["tool_calls"] == 0


def test_preview_refuses_nan_values(tmp_path):
    orchestrator = make_orchestrator(tmp_path, NonFinitePreviewBackend(float("nan")))
    orchestrator.register_tool("record_score", lambda score: score)

    result = orchestrator.preview(TaskManifold(objective="demo objective"))

    assert result["supported"] is False
    assert result["approvable"] is False
    assert result["status"] == "preview-malformed"
    assert "finite JSON values" in result["expected_halt_reason"]


def test_preview_refuses_infinite_values(tmp_path):
    orchestrator = make_orchestrator(tmp_path, NonFinitePreviewBackend(float("inf")))
    orchestrator.register_tool("record_score", lambda score: score)

    result = orchestrator.preview(TaskManifold(objective="demo objective"))

    assert result["supported"] is False
    assert result["approvable"] is False
    assert result["status"] == "preview-malformed"
    assert "finite JSON values" in result["expected_halt_reason"]


def test_preview_refuses_colliding_keys_after_normalization(tmp_path):
    orchestrator = make_orchestrator(tmp_path, CollidingKeysBackend())
    orchestrator.register_tool("echo_payload", lambda key: key)

    result = orchestrator.preview(TaskManifold(objective="demo objective"))

    assert result["supported"] is False
    assert result["approvable"] is False
    assert result["status"] == "preview-malformed"
    assert "duplicate keys" in result["expected_halt_reason"]


def test_preview_refuses_overlong_keys(tmp_path):
    orchestrator = make_orchestrator(tmp_path, OverlongKeyBackend())
    orchestrator.register_tool("echo_payload", lambda **kwargs: kwargs)

    result = orchestrator.preview(TaskManifold(objective="demo objective"))

    assert result["supported"] is False
    assert result["approvable"] is False
    assert result["status"] == "preview-malformed"
    assert "key exceeds maximum length" in result["expected_halt_reason"]


def test_preview_supports_valid_demo_echo_proposal(tmp_path):
    orchestrator = make_orchestrator(tmp_path, DemoBackend())
    orchestrator.register_tool("echo_text", lambda text: text)

    result = orchestrator.preview(TaskManifold(objective="demo objective"))

    assert result["supported"] is True
    assert result["approvable"] is True
    assert result["status"] == "preview"
    assert result["action"] == {
        "tool": "echo_text",
        "kwargs": {"text": "objective=demo objective"},
        "comment": "[DEV-ONLY] safe demo action — not a real provider response",
    }
    assert result["action_digest"]


def test_execute_halts_when_approved_tool_mismatch_is_detected(tmp_path):
    tool_calls = {"count": 0}

    def echo_text(text: str) -> str:
        tool_calls["count"] += 1
        return text

    def write_text(text: str) -> str:
        tool_calls["count"] += 1
        return text

    backend = DivergentActionBackend(
        preview_action={
            "tool": "echo_text",
            "kwargs": {"text": "preview"},
            "comment": "preview echo",
        },
        run_action={
            "tool": "write_text",
            "kwargs": {"text": "run"},
            "comment": "run write",
        },
    )
    orchestrator = make_orchestrator(tmp_path, backend)
    orchestrator.register_tool("echo_text", echo_text)
    orchestrator.register_tool("write_text", write_text)
    runtime = SovereignRuntime(orchestrator=orchestrator)

    preview = runtime.preview("demo objective")
    result = runtime.run("demo objective", expected_action_digest=preview["action_digest"])

    assert result["status"] == "halted"
    assert result["reason"] == "APPROVED_ACTION_MISMATCH"
    assert tool_calls["count"] == 0
    assert orchestrator.shield.execution_log() == []


def test_execute_halts_when_approved_kwargs_mismatch_is_detected(tmp_path):
    tool_calls = {"count": 0}

    def echo_text(text: str) -> str:
        tool_calls["count"] += 1
        return text

    backend = DivergentActionBackend(
        preview_action={
            "tool": "echo_text",
            "kwargs": {"text": "preview"},
            "comment": "preview echo",
        },
        run_action={
            "tool": "echo_text",
            "kwargs": {"text": "run"},
            "comment": "run echo",
        },
    )
    orchestrator = make_orchestrator(tmp_path, backend)
    orchestrator.register_tool("echo_text", echo_text)
    runtime = SovereignRuntime(orchestrator=orchestrator)

    preview = runtime.preview("demo objective")
    result = runtime.run("demo objective", expected_action_digest=preview["action_digest"])

    assert result["status"] == "halted"
    assert result["reason"] == "APPROVED_ACTION_MISMATCH"
    assert tool_calls["count"] == 0
    assert orchestrator.shield.execution_log() == []


def test_execute_requires_repreview_after_single_approved_tool(tmp_path):
    tool_calls = {"echo": 0, "wipe": 0}

    def echo_text(text: str) -> str:
        tool_calls["echo"] += 1
        return text

    def wipe_disk(target: str) -> str:
        tool_calls["wipe"] += 1
        return target

    approved_action = {
        "tool": "echo_text",
        "kwargs": {"text": "preview"},
        "comment": "approved preview echo",
    }
    backend = ApprovalScopeBackend(
        approved_action=approved_action,
        later_action={
            "tool": "wipe_disk",
            "kwargs": {"target": "/important"},
            "comment": "destructive follow-up",
        },
    )
    orchestrator = make_orchestrator(tmp_path, backend)
    orchestrator.register_tool("echo_text", echo_text)
    orchestrator.register_tool("wipe_disk", wipe_disk)
    runtime = SovereignRuntime(orchestrator=orchestrator)

    preview = runtime.preview("demo objective", risk_threshold=1.1)
    # Use risk_threshold=1.1 so the Soft Silence Clause does not fire when
    # no evaluator is registered and drift stays unchanged at 1.0 (UNMEASURED state).
    result = runtime.run(
        "demo objective",
        risk_threshold=1.1,
        expected_action_digest=preview["action_digest"],
    )

    assert preview["approvable"] is True
    assert result["status"] == "halted"
    assert result["reason"] == "APPROVAL_SCOPE_EXHAUSTED"
    assert result["required_action"] == "REPREVIEW_REQUIRED"
    assert tool_calls["echo"] == 1
    assert tool_calls["wipe"] == 0
    assert backend.calls == 2


def test_uppercase_action_digest_accepted(tmp_path):
    """An uppercase SHA-256 hex digest must be accepted equivalently to lowercase."""

    def echo_text(text=""):
        return f"echoed: {text}"

    backend = EchoPreviewBackend()
    orchestrator = make_orchestrator(tmp_path, backend)
    orchestrator.register_tool("echo_text", echo_text)
    runtime = SovereignRuntime(orchestrator=orchestrator)

    preview = runtime.preview("demo objective")
    lowercase_digest = preview["action_digest"]
    assert lowercase_digest == lowercase_digest.lower()

    # Pass the same digest in uppercase — must NOT be treated as invalid
    uppercase_digest = lowercase_digest.upper()
    result = runtime.run("demo objective", expected_action_digest=uppercase_digest)

    assert result["status"] in {"executed", "halted"}
    assert result.get("reason") != "INVALID_APPROVED_ACTION_DIGEST", (
        "Uppercase hex digest must not trigger INVALID_APPROVED_ACTION_DIGEST"
    )
