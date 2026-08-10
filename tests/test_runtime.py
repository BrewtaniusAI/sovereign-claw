from sovereign_claw.runtime import SovereignRuntime


class GoodOrchestrator:
    def run(self, objective: str):
        return {
            "tool": "analyze",
            "kwargs": {"objective": objective},
            "comment": "ok",
            "agent_id": "giles",
        }


class HaltOrchestrator:
    def run(self, objective: str):
        return {
            "tool": "HALT",
            "kwargs": {},
            "comment": "Policy violation",
            "agent_id": "orchestrator",
        }


class BadOrchestrator:
    def run(self, objective: str):
        return "not-a-dict"


class SideEffectOrchestrator:
    def __init__(self) -> None:
        self.run_calls = 0

    def run(self, objective: str):
        self.run_calls += 1
        return {
            "tool": "echo_text",
            "kwargs": {"objective": objective},
            "comment": "executed",
        }


class ExecuteOnlyOrchestrator:
    def __init__(self) -> None:
        self.execute_calls = 0

    def execute(self, manifold):
        self.execute_calls += 1
        return {
            "status": "executed",
            "trace_id": "trace-1",
            "steps": 1,
            "final_drift": 0.0,
            "drift_trajectory": [0.0],
            "provider": "demo",
            "policy_status": "constraint-gated",
        }


def test_runtime_returns_executed_action():
    runtime = SovereignRuntime(orchestrator=GoodOrchestrator())
    result = runtime.run("stabilize ai")

    assert result["status"] == "executed"
    assert result["action"]["tool"] == "analyze"
    assert result["action"]["kwargs"] == {"objective": "stabilize ai"}


def test_runtime_returns_halted_state():
    runtime = SovereignRuntime(orchestrator=HaltOrchestrator())
    result = runtime.run("stabilize ai")

    assert result["status"] == "halted"
    assert result["reason"] == "Policy violation"
    assert result["agent"] == "orchestrator"


def test_runtime_handles_invalid_orchestrator_response():
    runtime = SovereignRuntime(orchestrator=BadOrchestrator())
    result = runtime.run("stabilize ai")

    assert result["status"] == "error"
    assert result["reason"] == "Invalid orchestrator response"
    assert result["raw"] == "not-a-dict"


def test_preview_does_not_execute_legacy_run_orchestrator():
    orchestrator = SideEffectOrchestrator()
    runtime = SovereignRuntime(orchestrator=orchestrator)

    result = runtime.preview("stabilize ai")

    assert result["status"] == "preview-unsupported"
    assert result["supported"] is False
    assert result["tool_calls"] == 0
    assert orchestrator.run_calls == 0


def test_preview_does_not_fall_through_execute_without_preview_support():
    orchestrator = ExecuteOnlyOrchestrator()
    runtime = SovereignRuntime(orchestrator=orchestrator)

    result = runtime.preview("stabilize ai")

    assert result["status"] == "preview-unsupported"
    assert result["supported"] is False
    assert result["tool_calls"] == 0
    assert orchestrator.execute_calls == 0
