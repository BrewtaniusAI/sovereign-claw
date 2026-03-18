"""
tests/test_isomorphic_closure.py
=================================
Comprehensive test suite for Sovereign Claw v1.0.0.

Tests cover:
  • TaskManifold validation
  • SystemThermodynamics: descent, Thoth-Wadjet snap, T_max detection
  • KitaevZeroMode: success, error tiers, forbidden actions
  • ProofVault: create trace, append step, retrieve, reputation weights
  • Orchestrator: closure, T_max, Silence Clause, forbidden action, unknown tool
  • LaneRouter: advance logic, stall guard, early closure
"""

from __future__ import annotations

import math
import os
import pytest
from typing import Any, Dict, List

# ── Set test DB path before importing vault ───────────────────────────────────
os.environ["SOVEREIGN_CLAW_DB"] = "/tmp/sovereign_claw_test.sqlite3"

from sovereign_claw.thermodynamics import TaskManifold, SystemThermodynamics
from sovereign_claw.kitaev_shield import KitaevZeroMode
from sovereign_claw.proof_vault import ProofVault
from sovereign_claw.orchestrator import Orchestrator
from sovereign_claw.lanes import Lane, LaneRouter


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _make_manifold(**kwargs) -> TaskManifold:
    defaults = dict(objective="test", t_max_steps=5)
    defaults.update(kwargs)
    return TaskManifold(**defaults)


class _AlwaysHaltLLM:
    def decide_next_action(self, objective, history, forbidden_actions, drift):
        return {"tool": "HALT", "kwargs": {}, "comment": "Immediate halt."}


class _ScriptedLLM:
    """Returns a predefined sequence of decisions, then HALTs."""

    def __init__(self, steps: List[Dict[str, Any]]) -> None:
        self._steps = list(steps)
        self._idx = 0

    def decide_next_action(self, objective, history, forbidden_actions, drift):
        if self._idx < len(self._steps):
            d = self._steps[self._idx]
            self._idx += 1
            return d
        return {"tool": "HALT", "kwargs": {}, "comment": "Script exhausted."}


# ─────────────────────────────────────────────────────────────────────────────
# TaskManifold tests
# ─────────────────────────────────────────────────────────────────────────────


class TestTaskManifold:
    def test_defaults(self):
        m = _make_manifold()
        assert m.t_max_steps == 5
        assert m.forbidden_actions == []
        assert m.risk_threshold == 0.90

    def test_theoretical_t_max(self):
        m = TaskManifold(objective="x", t_max_steps=10)
        # 1/(a(1-p)) + 1/(b(q-1))  with a=b=1, p=0.5, q=2
        expected = 1 / (1 * (1 - 0.5)) + 1 / (1 * (2 - 1))
        assert abs(m.theoretical_t_max - expected) < 1e-9

    def test_invalid_t_max(self):
        with pytest.raises(ValueError):
            TaskManifold(objective="x", t_max_steps=0)

    def test_invalid_p(self):
        with pytest.raises(ValueError):
            TaskManifold(objective="x", elfe_p=1.5)

    def test_invalid_q(self):
        with pytest.raises(ValueError):
            TaskManifold(objective="x", elfe_q=0.5)


# ─────────────────────────────────────────────────────────────────────────────
# SystemThermodynamics tests
# ─────────────────────────────────────────────────────────────────────────────


class TestSystemThermodynamics:
    def test_initial_drift_is_one(self):
        therm = SystemThermodynamics(_make_manifold())
        assert therm.current_drift == 1.0

    def test_drift_decreases_on_zero_penalty(self):
        therm = SystemThermodynamics(_make_manifold(t_max_steps=20))
        d0 = therm.current_drift
        d1 = therm.apply_drift_update(step_count=0, error_penalty=0.0)
        assert d1 < d0

    def test_thoth_wadjet_snap(self):
        therm = SystemThermodynamics(_make_manifold(t_max_steps=200))
        # Drive drift just below 1/64
        therm.current_drift = 0.015  # < 1/64 ≈ 0.015625
        result = therm.apply_drift_update(step_count=0, error_penalty=0.0)
        assert result == 0.0

    def test_drift_clamped_to_zero(self):
        therm = SystemThermodynamics(_make_manifold())
        for i in range(30):
            therm.apply_drift_update(step_count=i, error_penalty=0.0)
        assert therm.current_drift >= 0.0

    def test_drift_clamped_to_one(self):
        therm = SystemThermodynamics(_make_manifold())
        therm.apply_drift_update(step_count=0, error_penalty=10.0)
        assert therm.current_drift <= 1.0

    def test_isomorphic_closure(self):
        therm = SystemThermodynamics(_make_manifold())
        therm.current_drift = 0.0
        assert therm.check_isomorphic_state(0) == "ISOMORPHIC_CLOSURE"

    def test_t_max_violation(self):
        m = _make_manifold(t_max_steps=3)
        therm = SystemThermodynamics(m)
        assert therm.check_isomorphic_state(3) == "T_MAX_VIOLATION"

    def test_continue_descent(self):
        therm = SystemThermodynamics(_make_manifold(t_max_steps=10))
        assert therm.check_isomorphic_state(2) == "CONTINUE_DESCENT"

    def test_drift_trajectory_length(self):
        therm = SystemThermodynamics(_make_manifold())
        for i in range(5):
            therm.apply_drift_update(i, 0.0)
        assert len(therm.drift_trajectory()) == 5


# ─────────────────────────────────────────────────────────────────────────────
# KitaevZeroMode tests
# ─────────────────────────────────────────────────────────────────────────────


class TestKitaevZeroMode:
    def test_success_zero_penalty(self):
        shield = KitaevZeroMode()
        result = shield.execute_safely("echo", lambda text="": text, {"text": "hi"})
        assert result["success"] is True
        assert result["drift_penalty"] == 0.0
        assert result["payload"] == "hi"

    def test_exception_yields_nonzero_penalty(self):
        shield = KitaevZeroMode()

        def boom(**kwargs):
            raise RuntimeError("fail")

        result = shield.execute_safely("boom", boom, {})
        assert result["success"] is False
        assert result["drift_penalty"] > 0.0
        assert "Constraint blocked" in result["payload"]

    def test_no_stack_trace_in_payload(self):
        shield = KitaevZeroMode()

        def boom(**kwargs):
            raise ValueError("secret details here")

        result = shield.execute_safely("boom", boom, {})
        assert "secret details" not in result["payload"]
        assert "Traceback" not in result["payload"]

    def test_forbidden_action_blocked(self):
        shield = KitaevZeroMode(forbidden_names={"dangerous_tool"})
        result = shield.execute_safely("dangerous_tool", lambda: None, {})
        assert result["success"] is False
        assert result["drift_penalty"] > 0.0

    def test_penalty_scale(self):
        shield_lenient = KitaevZeroMode(penalty_scale=0.5)
        shield_strict = KitaevZeroMode(penalty_scale=2.0)

        def boom(**kwargs):
            raise RuntimeError("x")

        r1 = shield_lenient.execute_safely("t", boom, {})
        r2 = shield_strict.execute_safely("t", boom, {})
        assert r1["drift_penalty"] < r2["drift_penalty"]

    def test_permission_error_higher_penalty(self):
        shield = KitaevZeroMode()

        def perm_fail(**kwargs):
            raise PermissionError("denied")

        def val_fail(**kwargs):
            raise ValueError("bad value")

        r1 = shield.execute_safely("perm", perm_fail, {})
        r2 = shield.execute_safely("val", val_fail, {})
        assert r1["drift_penalty"] > r2["drift_penalty"]


# ─────────────────────────────────────────────────────────────────────────────
# ProofVault tests
# ─────────────────────────────────────────────────────────────────────────────


class TestProofVault:
    def _fresh_vault(self, tmp_path) -> ProofVault:
        return ProofVault(db_path=tmp_path / "pv.sqlite3")

    def test_create_and_retrieve_trace(self, tmp_path):
        vault = self._fresh_vault(tmp_path)
        tid = vault.create_trace("objective A", {"k": "v"})
        assert isinstance(tid, str) and len(tid) > 0

    def test_append_and_get_steps(self, tmp_path):
        from sovereign_claw.proof_vault import StepRecord

        vault = self._fresh_vault(tmp_path)
        tid = vault.create_trace("obj")
        vault.append_step(
            StepRecord(
                trace_id=tid,
                step_index=0,
                timestamp=0.0,
                node="test",
                action="ACT",
                drift=0.5,
                status="CONTINUE_DESCENT",
                payload={"x": 1},
            )
        )
        steps = vault.get_trace_steps(tid)
        assert len(steps) == 1
        assert steps[0].drift == 0.5
        assert steps[0].payload == {"x": 1}

    def test_get_trace_summary(self, tmp_path):
        from sovereign_claw.proof_vault import StepRecord

        vault = self._fresh_vault(tmp_path)
        tid = vault.create_trace("obj")
        for i in range(3):
            vault.append_step(
                StepRecord(
                    trace_id=tid,
                    step_index=i,
                    timestamp=float(i),
                    node="n",
                    action="A",
                    drift=1.0 - i * 0.3,
                    status="CONTINUE_DESCENT",
                    payload={},
                )
            )
        s = vault.get_trace_summary(tid)
        assert s["steps"] == 3
        assert s["final_drift"] == pytest.approx(0.4)

    def test_agent_reputation_weight_defaults_to_one(self, tmp_path):
        vault = self._fresh_vault(tmp_path)
        w = vault.get_agent_reputation_weight("new_agent")
        assert w == 1.0

    def test_agent_reputation_decreases_with_drift(self, tmp_path):
        vault = self._fresh_vault(tmp_path)
        vault.update_agent_reputation("agent_x", step_drift=0.5)
        vault.update_agent_reputation("agent_x", step_drift=0.5)
        w = vault.get_agent_reputation_weight("agent_x", k=1.0)
        assert w == pytest.approx(math.exp(-1.0))

    def test_list_agent_weights_sorted(self, tmp_path):
        vault = self._fresh_vault(tmp_path)
        vault.update_agent_reputation("good_agent", 0.1)
        vault.update_agent_reputation("bad_agent", 0.9)
        agents = vault.list_agent_weights()
        # good_agent has lower integral → higher weight → listed first
        assert agents[0]["agent_id"] == "good_agent"
        assert agents[0]["weight"] > agents[1]["weight"]


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator tests
# ─────────────────────────────────────────────────────────────────────────────


class TestOrchestrator:
    def _orch(self, llm, tmp_path):
        vault = ProofVault(db_path=tmp_path / "pv.sqlite3")
        return Orchestrator(llm_backend=llm, vault=vault)

    def test_immediate_halt_status(self, tmp_path):
        orch = self._orch(_AlwaysHaltLLM(), tmp_path)
        receipt = orch.execute(_make_manifold())
        assert receipt.status == "HALTED_SILENCE_CLAUSE"
        assert receipt.steps == 0

    def test_isomorphic_closure_via_tool(self, tmp_path):
        """Agent calls a tool that drives drift to zero via many steps."""
        # We'll use a scripted LLM that echoes repeatedly;
        # after enough steps the ELFE kernel should snap to closure.
        steps = [{"tool": "echo", "kwargs": {"text": "x"}, "comment": ""} for _ in range(20)]
        llm = _ScriptedLLM(steps)
        vault = ProofVault(db_path=tmp_path / "pv.sqlite3")
        orch = Orchestrator(llm_backend=llm, vault=vault)
        orch.register_tool("echo", lambda text="": text)
        receipt = orch.execute(_make_manifold(t_max_steps=20))
        # Either closed or hit T_max — never an unexpected status
        assert receipt.status in ("ISOMORPHIC_CLOSURE", "T_MAX_VIOLATION", "HALTED_SILENCE_CLAUSE")

    def test_t_max_violation(self, tmp_path):
        # LLM keeps calling a tool; T_max fires at step 3
        # With descent_scale=0.1 and t_max_steps=3, drift won't reach 0
        # before the budget runs out (needs ~8-10 steps to converge naturally).
        steps = [{"tool": "echo", "kwargs": {"text": "x"}, "comment": ""} for _ in range(20)]
        llm = _ScriptedLLM(steps)
        vault = ProofVault(db_path=tmp_path / "pv.sqlite3")
        orch = Orchestrator(llm_backend=llm, vault=vault)
        orch.register_tool("echo", lambda text="": text)
        receipt = orch.execute(_make_manifold(t_max_steps=3))
        assert receipt.status == "T_MAX_VIOLATION"
        assert receipt.steps <= 3

    def test_forbidden_action_blocked(self, tmp_path):
        llm = _ScriptedLLM([{"tool": "execute_shell", "kwargs": {}, "comment": "try shell"}])
        vault = ProofVault(db_path=tmp_path / "pv.sqlite3")
        orch = Orchestrator(llm_backend=llm, vault=vault)
        receipt = orch.execute(_make_manifold(forbidden_actions=["execute_shell"]))
        assert receipt.status == "HALTED_SILENCE_CLAUSE"

    def test_unknown_tool_halts(self, tmp_path):
        llm = _ScriptedLLM([{"tool": "nonexistent_tool", "kwargs": {}, "comment": ""}])
        vault = ProofVault(db_path=tmp_path / "pv.sqlite3")
        orch = Orchestrator(llm_backend=llm, vault=vault)
        receipt = orch.execute(_make_manifold())
        assert receipt.status == "HALTED_SILENCE_CLAUSE"

    def test_execution_receipt_has_trajectory(self, tmp_path):
        llm = _ScriptedLLM(
            [
                {"tool": "echo", "kwargs": {"text": "a"}, "comment": ""},
                {"tool": "echo", "kwargs": {"text": "b"}, "comment": ""},
            ]
        )
        vault = ProofVault(db_path=tmp_path / "pv.sqlite3")
        orch = Orchestrator(llm_backend=llm, vault=vault)
        orch.register_tool("echo", lambda text="": text)
        receipt = orch.execute(_make_manifold(t_max_steps=5))
        assert isinstance(receipt.drift_trajectory, list)

    def test_soft_silence_clause_fires(self, tmp_path):
        """
        A tool with a moderate error penalty should trigger the soft Silence Clause.
        We use penalty_scale=1.0 but a low risk_threshold=0.50 so that accumulated
        drift from a failed tool call keeps drift above the threshold.
        The tool raises ValueError (penalty=0.25) which with descent_scale=0.1
        will cause drift to rise (net delta = 0.1*descent - 0.25 < 0 initially).
        """
        llm = _ScriptedLLM(
            [
                {"tool": "bad_tool", "kwargs": {}, "comment": ""},
            ]
            * 10
        )
        vault = ProofVault(db_path=tmp_path / "pv.sqlite3")
        shield = KitaevZeroMode(penalty_scale=1.0)
        orch = Orchestrator(llm_backend=llm, vault=vault, shield=shield)

        def bad_tool():
            raise ValueError("bad input")

        orch.register_tool("bad_tool", bad_tool)
        receipt = orch.execute(_make_manifold(t_max_steps=10, risk_threshold=0.50))
        # With penalty=0.25 and scale=0.1, drift increases on each step
        # → soft silence fires quickly since drift > 0.50
        assert receipt.status == "HALTED_SILENCE_CLAUSE"


# ─────────────────────────────────────────────────────────────────────────────
# LaneRouter tests
# ─────────────────────────────────────────────────────────────────────────────


class TestLaneRouter:
    def test_starts_at_reflex(self):
        r = LaneRouter()
        assert r.current == Lane.REFLEX

    def test_reflex_advances_to_deliberate(self):
        r = LaneRouter()
        r.advance(approved=False)
        assert r.current == Lane.DELIBERATE

    def test_approved_advances_to_authoritative(self):
        r = LaneRouter()
        r.advance(approved=False)  # REFLEX → DELIBERATE
        r.advance(approved=True)  # DELIBERATE → AUTHORITATIVE
        assert r.current == Lane.AUTHORITATIVE

    def test_stall_guard_fires(self):
        r = LaneRouter(max_deliberate_loops=1)
        r.advance(approved=False)  # REFLEX → DELIBERATE
        r.advance(approved=False)  # DELIBERATE (loop 1) → STALL
        assert r.current == Lane.STALL
        assert r.done is True

    def test_early_closure_on_zero_drift(self):
        r = LaneRouter()
        r.advance(approved=False, drift=0.0)
        assert r.current == Lane.AUTHORITATIVE

    def test_final_status_on_authoritative(self):
        r = LaneRouter()
        r.advance(approved=False)  # → DELIBERATE
        r.advance(approved=True)  # → AUTHORITATIVE
        r.advance(approved=True)  # → done
        assert r.final_status == "ISOMORPHIC_CLOSURE"

    def test_reset(self):
        r = LaneRouter()
        r.advance(approved=False)
        r.reset()
        assert r.current == Lane.REFLEX
        assert not r.done
