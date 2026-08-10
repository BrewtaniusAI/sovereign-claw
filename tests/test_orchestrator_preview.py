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
    assert tool_calls["count"] == 0
    assert orchestrator.shield.execution_log() == []


def test_preview_refuses_malformed_model_output(tmp_path):
    orchestrator = make_orchestrator(tmp_path, MalformedPreviewBackend())

    result = orchestrator.preview(TaskManifold(objective="demo objective"))

    assert result["supported"] is False
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

    assert result["supported"] is False
    assert result["status"] == "preview-forbidden"
    assert result["tool_calls"] == 0
    assert tool_calls["count"] == 0


def test_preview_refuses_unknown_tool(tmp_path):
    orchestrator = make_orchestrator(tmp_path, UnknownToolBackend())

    result = orchestrator.preview(TaskManifold(objective="demo objective"))

    assert result["supported"] is False
    assert result["status"] == "preview-unknown-tool"
    assert result["tool_calls"] == 0


def test_preview_supports_valid_demo_echo_proposal(tmp_path):
    orchestrator = make_orchestrator(tmp_path, DemoBackend())
    orchestrator.register_tool("echo_text", lambda text: text)

    result = orchestrator.preview(TaskManifold(objective="demo objective"))

    assert result["supported"] is True
    assert result["status"] == "preview"
    assert result["action"] == {
        "tool": "echo_text",
        "kwargs": {"text": "objective=demo objective"},
        "comment": "[DEV-ONLY] safe demo action — not a real provider response",
    }
