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
