from __future__ import annotations

from pathlib import Path

from sovereign_claw import EventStream, PolicyEngine, ProofVault, StepRecord


def test_policy_engine_blocks_forbidden_tool() -> None:
    engine = PolicyEngine(forbidden_tools={"shell_exec"})
    decision = engine.evaluate({"tool": "shell_exec", "kwargs": {"cmd": "id"}})
    assert decision.allowed is False
    assert any("forbidden" in reason for reason in decision.reasons)


def test_event_stream_replays_trace(tmp_path: Path) -> None:
    stream = EventStream(tmp_path / "events.jsonl")
    vault = ProofVault(db_path=tmp_path / "proof.sqlite3", event_stream=stream)
    trace_id = vault.create_trace("demo-objective", meta={"lane": "green"})
    vault.append_step(
        StepRecord(
            trace_id=trace_id,
            step_index=0,
            timestamp=123.456,
            node="giles",
            action="HALT",
            drift=0.0,
            status="ISOMORPHIC_CLOSURE",
            payload={"note": "done"},
        )
    )

    replay = stream.replay(trace_id)
    assert replay["created"] is True
    assert replay["objective"] == "demo-objective"
    assert replay["meta"]["lane"] == "green"
    assert replay["steps"][0]["status"] == "ISOMORPHIC_CLOSURE"
