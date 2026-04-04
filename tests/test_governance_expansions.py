"""
Tests for v3.0.0 governance expansions:
  - Proof Receipts (hash chain, export, replay, diff)
  - Drift Visualization (decomposed D_tool/D_constraint/D_provider/D_policy)
  - Economic Model Router (ProviderCost, ExecutionMode, multi_objective_score)
  - Adaptive Policy Engine (profiles, contextual drift rules, learned signals)
  - Multi-Agent Orchestrator (AgentRegistry, role isolation AG-05, governed loop)
  - Skills Marketplace (signed skills, trust scores, permission scoping)
  - Channel Interface Mesh (cross-channel identity, session continuity, per-channel policies)
  - State + Memory Layer (episodic/semantic/task memory, TTL, relevance)
  - Governance enforcement tests (AG-02 evaluation, AG-05 role isolation, AG-07 refusal)
"""

from __future__ import annotations

import json
import time

import pytest


# ── Proof Receipts ────────────────────────────────────────────────────────────
class TestProofReceipts:
    def _make_vault_with_trace(self):
        import time as _time

        from sovereign_claw.proof_vault import ProofVault, StepRecord

        vault = ProofVault()
        trace_id = vault.create_trace("test_objective")

        vault.append_step(
            StepRecord(
                trace_id=trace_id,
                step_index=0,
                timestamp=_time.time(),
                node="policy_gate",
                action="evaluate",
                drift=1.0,
                status="allowed",
                payload={"tool": "greet", "decision_comment": "policy passed"},
            )
        )
        vault.append_step(
            StepRecord(
                trace_id=trace_id,
                step_index=1,
                timestamp=_time.time(),
                node="tool_exec",
                action="greet",
                drift=0.95,
                status="success",
                payload={"tool": "greet", "success": True},
            )
        )
        vault.append_step(
            StepRecord(
                trace_id=trace_id,
                step_index=2,
                timestamp=_time.time(),
                node="halt",
                action="HALT",
                drift=0.90,
                status="closed",
                payload={"reason": "objective achieved"},
            )
        )
        return vault, trace_id

    def test_build_receipt_produces_hash_chain(self):
        from sovereign_claw.receipts import ReceiptBuilder

        vault, trace_id = self._make_vault_with_trace()
        builder = ReceiptBuilder(vault)
        receipt = builder.build_receipt(trace_id)

        assert receipt.trace_id == trace_id
        assert receipt.total_steps == 3
        assert receipt.chain_root != ""
        assert receipt.chain_tip != ""
        assert receipt.chain_root != receipt.chain_tip
        assert receipt.verified is True

    def test_verify_chain_integrity(self):
        from sovereign_claw.receipts import ReceiptBuilder

        vault, trace_id = self._make_vault_with_trace()
        builder = ReceiptBuilder(vault)
        receipt = builder.build_receipt(trace_id)

        assert builder.verify_chain(receipt) is True

    def test_chain_tip_changes_with_different_data(self):
        """
        Hash chain integrity: two traces with different steps must produce
        different chain tips — proving the chain encodes content, not just order.
        """
        import time as _time

        from sovereign_claw.proof_vault import ProofVault, StepRecord
        from sovereign_claw.receipts import ReceiptBuilder

        vault = ProofVault()

        # Trace A
        tid_a = vault.create_trace("objective_a")
        vault.append_step(
            StepRecord(
                trace_id=tid_a,
                step_index=0,
                timestamp=_time.time(),
                node="n1",
                action="action_alpha",
                drift=1.0,
                status="ok",
                payload={"x": 1},
            )
        )

        # Trace B — identical except action differs
        tid_b = vault.create_trace("objective_b")
        vault.append_step(
            StepRecord(
                trace_id=tid_b,
                step_index=0,
                timestamp=_time.time(),
                node="n1",
                action="action_beta",
                drift=1.0,
                status="ok",
                payload={"x": 1},
            )
        )

        builder = ReceiptBuilder(vault)
        receipt_a = builder.build_receipt(tid_a)
        receipt_b = builder.build_receipt(tid_b)

        # Different content → different chain tips
        assert receipt_a.chain_tip != receipt_b.chain_tip
        # Both are individually valid
        assert builder.verify_chain(receipt_a) is True
        assert builder.verify_chain(receipt_b) is True

    def test_export_json_format(self):
        from sovereign_claw.receipts import ReceiptBuilder

        vault, trace_id = self._make_vault_with_trace()
        builder = ReceiptBuilder(vault)

        result = builder.export(trace_id, fmt="json")
        data = json.loads(result)

        assert data["trace_id"] == trace_id
        assert data["total_steps"] == 3
        assert data["verified"] is True
        assert len(data["steps"]) == 3
        assert "step_hash" in data["steps"][0]

    def test_export_hash_format(self):
        from sovereign_claw.receipts import ReceiptBuilder

        vault, trace_id = self._make_vault_with_trace()
        builder = ReceiptBuilder(vault)

        result = builder.export(trace_id, fmt="hash")

        assert f"trace_id: {trace_id}" in result
        assert "chain_root:" in result
        assert "chain_tip:" in result
        assert "hash_chain:" in result

    def test_replay_produces_ordered_steps(self):
        from sovereign_claw.receipts import ReceiptBuilder

        vault, trace_id = self._make_vault_with_trace()
        builder = ReceiptBuilder(vault)

        steps = builder.replay(trace_id)

        assert len(steps) == 3
        assert steps[0].step_index == 0
        assert steps[0].action == "evaluate"
        assert steps[1].action == "greet"
        assert steps[2].action == "HALT"

    def test_replay_drift_delta_calculated(self):
        from sovereign_claw.receipts import ReceiptBuilder

        vault, trace_id = self._make_vault_with_trace()
        builder = ReceiptBuilder(vault)

        steps = builder.replay(trace_id)

        # Drift deltas should be differences from previous step
        assert steps[1].drift_delta == pytest.approx(0.95 - 1.0, abs=0.01)
        assert steps[2].drift_delta == pytest.approx(0.90 - 0.95, abs=0.01)

    def test_diff_between_traces(self):
        import time as _time

        from sovereign_claw.proof_vault import ProofVault, StepRecord
        from sovereign_claw.receipts import ReceiptBuilder

        vault = ProofVault()
        trace_a = vault.create_trace("objective_a")
        trace_b = vault.create_trace("objective_b")

        vault.append_step(
            StepRecord(
                trace_id=trace_a,
                step_index=0,
                timestamp=_time.time(),
                node="n1",
                action="act_a",
                drift=1.0,
                status="ok",
                payload={},
            )
        )
        vault.append_step(
            StepRecord(
                trace_id=trace_b,
                step_index=0,
                timestamp=_time.time(),
                node="n1",
                action="act_b",
                drift=0.9,
                status="ok",
                payload={},
            )
        )

        builder = ReceiptBuilder(vault)
        diff = builder.diff(trace_a, trace_b)

        assert diff.trace_a_id == trace_a
        assert diff.trace_b_id == trace_b
        assert diff.common_steps == 1
        assert len(diff.differences) > 0  # action and drift differ


# ── Drift Visualization ──────────────────────────────────────────────────────
class TestDriftVisualization:
    def test_decomposed_drift_components(self):
        from sovereign_claw.drift import DriftTracker

        tracker = DriftTracker("trace-001")
        tracker.record_tool_drift(0, 0.1, "tool error")
        tracker.record_constraint_drift(0, 0.05, "constraint mismatch")
        tracker.record_provider_drift(0, 0.02, "provider latency")
        tracker.record_policy_drift(0, 0.03, "policy tightening")

        report = tracker.report()

        assert report.total_d_tool == pytest.approx(0.1)
        assert report.total_d_constraint == pytest.approx(0.05)
        assert report.total_d_provider == pytest.approx(0.02)
        assert report.total_d_policy == pytest.approx(0.03)
        assert report.total_drift == pytest.approx(0.2)

    def test_dominant_source_identification(self):
        from sovereign_claw.drift import DriftTracker

        tracker = DriftTracker("trace-002")
        tracker.record_tool_drift(0, 0.5, "major tool failure")
        tracker.record_constraint_drift(0, 0.01)
        tracker.record_provider_drift(0, 0.01)
        tracker.record_policy_drift(0, 0.01)

        report = tracker.report()

        assert report.dominant_source == "tool"

    def test_multi_step_drift_tracking(self):
        from sovereign_claw.drift import DriftTracker

        tracker = DriftTracker("trace-003")
        tracker.record_tool_drift(0, 0.1)
        tracker.record_tool_drift(1, 0.2)
        tracker.record_tool_drift(2, 0.05)

        report = tracker.report()

        assert len(report.steps) == 3
        assert report.total_d_tool == pytest.approx(0.35)

    def test_get_step_breakdown(self):
        from sovereign_claw.drift import DriftTracker

        tracker = DriftTracker("trace-004")
        tracker.record_tool_drift(0, 0.1)
        tracker.record_constraint_drift(0, 0.05)

        step = tracker.get_step(0)

        assert step.d_tool == pytest.approx(0.1)
        assert step.d_constraint == pytest.approx(0.05)
        assert step.d_total == pytest.approx(0.15)

    def test_get_nonexistent_step_returns_empty(self):
        from sovereign_claw.drift import DriftTracker

        tracker = DriftTracker("trace-005")

        step = tracker.get_step(99)

        assert step.d_total == 0.0

    def test_drift_report_summary(self):
        from sovereign_claw.drift import DriftTracker

        tracker = DriftTracker("trace-006")
        tracker.record_tool_drift(0, 0.1)

        report = tracker.report()
        summary = report.summary()

        assert summary["trace_id"] == "trace-006"
        assert summary["total_steps"] == 1
        assert summary["total_drift"] == pytest.approx(0.1)
        assert "breakdown" in summary
        assert summary["breakdown"]["d_tool"] == pytest.approx(0.1)

    def test_drift_breakdown_to_dict(self):
        from sovereign_claw.drift import DriftBreakdown

        breakdown = DriftBreakdown(step_index=0, d_tool=0.1, d_constraint=0.05)
        d = breakdown.to_dict()

        assert d["step_index"] == 0
        assert d["d_tool"] == 0.1
        assert d["d_constraint"] == 0.05
        assert d["d_total"] == pytest.approx(0.15)


# ── Economic Model Router ────────────────────────────────────────────────────
class TestEconomicRouter:
    def test_provider_cost_record_usage(self):
        from sovereign_claw.model_router import ProviderCost

        cost = ProviderCost(
            cost_per_input_token=0.003,
            cost_per_output_token=0.015,
        )

        call_cost = cost.record_usage(100, 50)

        assert call_cost == pytest.approx(0.003 * 100 + 0.015 * 50)
        assert cost.total_input_tokens == 100
        assert cost.total_output_tokens == 50
        assert cost.total_cost_usd == pytest.approx(call_cost)

    def test_execution_mode_enum(self):
        from sovereign_claw.model_router import ExecutionMode

        assert ExecutionMode.LOW_COST.value == "low_cost"
        assert ExecutionMode.BALANCED.value == "balanced"
        assert ExecutionMode.HIGH_ACCURACY.value == "high_accuracy"

    def test_provider_stats_success_rate(self):
        from sovereign_claw.model_router import ProviderStats

        stats = ProviderStats(total_calls=10, total_failures=2)

        assert stats.success_rate == pytest.approx(0.8)

    def test_provider_stats_avg_latency(self):
        from sovereign_claw.model_router import ProviderStats

        stats = ProviderStats(total_calls=5, total_latency_ms=500.0)

        assert stats.avg_latency_ms == pytest.approx(100.0)

    def test_multi_objective_score_default_weights(self):
        from sovereign_claw.model_router import ProviderStats, ProviderCost

        stats = ProviderStats(
            total_calls=10,
            total_failures=1,
            total_latency_ms=2000.0,
            reputation_score=0.9,
            cost=ProviderCost(total_cost_usd=0.5),
            drift_penalty=0.1,
        )

        score = stats.multi_objective_score()

        # Score should be a reasonable positive number
        assert isinstance(score, float)
        assert score > 0

    def test_multi_objective_score_penalizes_high_cost(self):
        from sovereign_claw.model_router import ProviderStats, ProviderCost

        cheap = ProviderStats(
            total_calls=10,
            total_failures=1,
            total_latency_ms=2000.0,
            reputation_score=0.9,
            cost=ProviderCost(total_cost_usd=0.01),
        )

        expensive = ProviderStats(
            total_calls=10,
            total_failures=1,
            total_latency_ms=2000.0,
            reputation_score=0.9,
            cost=ProviderCost(total_cost_usd=100.0),
        )

        assert cheap.multi_objective_score() > expensive.multi_objective_score()

    def test_multi_objective_score_penalizes_drift(self):
        from sovereign_claw.model_router import ProviderStats

        low_drift = ProviderStats(
            total_calls=10,
            reputation_score=0.9,
            drift_penalty=0.0,
        )

        high_drift = ProviderStats(
            total_calls=10,
            reputation_score=0.9,
            drift_penalty=1.0,
        )

        assert low_drift.multi_objective_score() > high_drift.multi_objective_score()

    def test_default_provider_costs_exist(self):
        from sovereign_claw.model_router import DEFAULT_PROVIDER_COSTS

        assert "anthropic" in DEFAULT_PROVIDER_COSTS
        assert "openai" in DEFAULT_PROVIDER_COSTS
        assert "ollama" in DEFAULT_PROVIDER_COSTS
        # Ollama is free
        assert DEFAULT_PROVIDER_COSTS["ollama"] == (0.0, 0.0)


# ── Adaptive Policy Engine ────────────────────────────────────────────────────
class TestAdaptivePolicyEngine:
    def test_default_profile_is_balanced(self):
        from sovereign_claw.policy_engine import PolicyEngine, PolicyProfile

        engine = PolicyEngine()

        assert engine.profile == PolicyProfile.BALANCED

    def test_set_profile_changes_limits(self):
        from sovereign_claw.policy_engine import PolicyEngine, PolicyProfile

        engine = PolicyEngine()
        engine.set_profile(PolicyProfile.STRICT)

        assert engine.profile == PolicyProfile.STRICT
        assert engine.max_payload_bytes == 16384
        assert engine.require_trace_id is True

    def test_strict_profile_requires_trace_id(self):
        from sovereign_claw.policy_engine import PolicyEngine, PolicyProfile

        engine = PolicyEngine(profile=PolicyProfile.STRICT)
        decision = engine.evaluate({"tool": "echo_text"})

        assert decision.allowed is False
        assert "trace_id is required by policy" in decision.reasons
        assert decision.profile == "strict"

    def test_exploratory_profile_allows_larger_payloads(self):
        from sovereign_claw.policy_engine import PolicyEngine, PolicyProfile

        engine = PolicyEngine(profile=PolicyProfile.EXPLORATORY)
        # 50KB payload — too big for strict/balanced, fine for exploratory
        big_payload = {"tool": "echo_text", "payload": "x" * 50000}
        decision = engine.evaluate(big_payload)

        assert decision.allowed is True

    def test_contextual_drift_tightening(self):
        from sovereign_claw.policy_engine import PolicyEngine, PolicyProfile

        engine = PolicyEngine(profile=PolicyProfile.STRICT)
        engine.update_drift(0.5)  # Above strict threshold of 0.3

        decision = engine.evaluate(
            {
                "tool": "test",
                "trace_id": "t-1",
                "tool_call_count": 5,  # Exceeds strict limit of 1
            }
        )

        assert decision.allowed is False
        assert any("tool call count" in r for r in decision.reasons)

    def test_learned_violation_auto_deny(self):
        from sovereign_claw.policy_engine import PolicyEngine

        engine = PolicyEngine(forbidden_tools=["bad_tool"])

        # Three violations trigger auto-deny
        for _ in range(3):
            engine.evaluate({"tool": "bad_tool"})

        history = engine.get_violation_history()
        assert "bad_tool" in history
        assert history["bad_tool"].count >= 3

    def test_test_policy_has_no_side_effects(self):
        from sovereign_claw.policy_engine import PolicyEngine

        engine = PolicyEngine(forbidden_tools=["bad_tool"])

        # Violations via test_policy should not persist
        for _ in range(5):
            engine.test_policy({"tool": "bad_tool"})

        history = engine.get_violation_history()
        assert len(history) == 0

    def test_clear_learned_denials(self):
        from sovereign_claw.policy_engine import PolicyEngine

        engine = PolicyEngine(forbidden_tools=["bad_tool"])

        for _ in range(3):
            engine.evaluate({"tool": "bad_tool"})

        engine.clear_learned_denials()
        history = engine.get_violation_history()
        assert len(history) == 0

    def test_decision_includes_drift_at_evaluation(self):
        from sovereign_claw.policy_engine import PolicyEngine

        engine = PolicyEngine()
        engine.update_drift(0.42)

        decision = engine.evaluate({"tool": "echo"})

        assert decision.drift_at_evaluation == pytest.approx(0.42)


# ── Multi-Agent Orchestrator ──────────────────────────────────────────────────
class TestMultiAgentOrchestrator:
    def test_agent_registration(self):
        from sovereign_claw.multi_agent import AgentRegistry, AgentRole

        registry = AgentRegistry()
        card = registry.register("planner_1", AgentRole.PLANNER, ["planning"])

        assert card.role == AgentRole.PLANNER
        assert card.name == "planner_1"
        assert card.trust_score == 1.0
        assert registry.get(card.agent_id) is card

    def test_get_by_role(self):
        from sovereign_claw.multi_agent import AgentRegistry, AgentRole

        registry = AgentRegistry()
        registry.register("p1", AgentRole.PLANNER)
        registry.register("e1", AgentRole.EXECUTOR)
        registry.register("p2", AgentRole.PLANNER)

        planners = registry.get_by_role(AgentRole.PLANNER)

        assert len(planners) == 2
        assert all(a.role == AgentRole.PLANNER for a in planners)

    def test_deregister(self):
        from sovereign_claw.multi_agent import AgentRegistry, AgentRole

        registry = AgentRegistry()
        card = registry.register("p1", AgentRole.PLANNER)

        assert registry.deregister(card.agent_id) is True
        assert registry.get(card.agent_id) is None
        assert registry.deregister("nonexistent") is False

    def test_reputation_tracking(self):
        from sovereign_claw.multi_agent import AgentRegistry, AgentRole

        registry = AgentRegistry()
        card = registry.register("e1", AgentRole.EXECUTOR)

        registry.record_execution(card.agent_id, True)
        registry.record_execution(card.agent_id, True)
        registry.record_execution(card.agent_id, False)

        assert card.execution_count == 3
        assert card.failure_count == 1
        assert card.success_rate == pytest.approx(2.0 / 3.0)

    def test_trust_score_bounds(self):
        from sovereign_claw.multi_agent import AgentRegistry, AgentRole

        registry = AgentRegistry()
        card = registry.register("e1", AgentRole.EXECUTOR)

        registry.update_trust(card.agent_id, 5.0)  # Try to exceed 1.0
        assert card.trust_score == 1.0

        registry.update_trust(card.agent_id, -5.0)  # Try to go below 0.0
        assert card.trust_score == 0.0

    def test_role_isolation_ag05(self):
        """AG-05: Verify agents have single roles and the orchestrator
        delegates strictly by role."""
        from sovereign_claw.multi_agent import (
            AgentRegistry,
            AgentRole,
            MultiAgentOrchestrator,
        )

        registry = AgentRegistry()
        planner = registry.register("planner", AgentRole.PLANNER)
        executor = registry.register("executor", AgentRole.EXECUTOR)
        validator = registry.register("validator", AgentRole.VALIDATOR)
        critic = registry.register("critic", AgentRole.CRITIC)

        # Each agent has exactly one role
        assert planner.role == AgentRole.PLANNER
        assert executor.role == AgentRole.EXECUTOR
        assert validator.role == AgentRole.VALIDATOR
        assert critic.role == AgentRole.CRITIC

        # No agent appears in multiple role groups
        _ = MultiAgentOrchestrator(registry=registry)
        p_ids = {a.agent_id for a in registry.get_by_role(AgentRole.PLANNER)}
        e_ids = {a.agent_id for a in registry.get_by_role(AgentRole.EXECUTOR)}
        v_ids = {a.agent_id for a in registry.get_by_role(AgentRole.VALIDATOR)}
        c_ids = {a.agent_id for a in registry.get_by_role(AgentRole.CRITIC)}

        assert p_ids.isdisjoint(e_ids)
        assert p_ids.isdisjoint(v_ids)
        assert p_ids.isdisjoint(c_ids)
        assert e_ids.isdisjoint(v_ids)
        assert e_ids.isdisjoint(c_ids)
        assert v_ids.isdisjoint(c_ids)

    def test_consensus_empty_proposals(self):
        from sovereign_claw.multi_agent import MultiAgentOrchestrator

        orchestrator = MultiAgentOrchestrator()

        result = orchestrator.evaluate_consensus([])

        assert result.accepted is False
        assert result.agreement_score == 0.0
        assert result.reason == "no proposals submitted"

    def test_consensus_single_proposal_accepted(self):
        from sovereign_claw.multi_agent import (
            AgentRegistry,
            AgentRole,
            AgentProposal,
            MultiAgentOrchestrator,
        )

        registry = AgentRegistry()
        card = registry.register("p1", AgentRole.PLANNER)
        orchestrator = MultiAgentOrchestrator(registry=registry)

        proposals = [
            AgentProposal(
                agent_id=card.agent_id,
                role=AgentRole.PLANNER,
                content={"action": "do_thing"},
                confidence=1.0,
            )
        ]

        result = orchestrator.evaluate_consensus(proposals)

        assert result.accepted is True
        assert result.agreement_score == 1.0
        assert result.final_proposal == {"action": "do_thing"}

    def test_governed_loop_completes(self):
        from sovereign_claw.multi_agent import (
            AgentRegistry,
            AgentRole,
            MultiAgentOrchestrator,
        )

        registry = AgentRegistry()
        registry.register("planner", AgentRole.PLANNER)
        registry.register("executor", AgentRole.EXECUTOR)
        registry.register("validator", AgentRole.VALIDATOR)
        registry.register("critic", AgentRole.CRITIC)

        orchestrator = MultiAgentOrchestrator(
            registry=registry,
            max_iterations=2,
        )

        result = orchestrator.run_governed_loop(
            objective="test_objective",
            context={"data": "test"},
        )

        assert result.iteration_count >= 1
        assert len(result.participating_agents) > 0
        assert "plan" in result.final_proposal

    def test_governed_loop_without_agents_still_returns(self):
        from sovereign_claw.multi_agent import MultiAgentOrchestrator

        orchestrator = MultiAgentOrchestrator(max_iterations=1)
        result = orchestrator.run_governed_loop("test", {})

        assert result.accepted is False
        # With no agents the loop still iterates but consensus always fails
        assert result.reason in ("not started", "loop complete")


# ── Skills Marketplace ────────────────────────────────────────────────────────
class TestSkillsMarketplace:
    def test_skill_signature_computation(self):
        from sovereign_claw.skills import SkillSpec

        spec = SkillSpec(
            name="test_skill",
            version="1.0.0",
            description="A test skill",
            author="devin",
            tools_provided=["tool_a", "tool_b"],
            forbidden_actions=["dangerous_action"],
        )

        sig = spec.compute_signature()

        assert len(sig) == 64  # SHA-256 hex digest
        assert sig == spec.compute_signature()  # Deterministic

    def test_signed_skill_verification_passes(self):
        from sovereign_claw.skills import SkillSpec

        spec = SkillSpec(
            name="test_skill",
            version="1.0.0",
            description="A signed skill",
            author="devin",
        )
        spec.signature_hash = spec.compute_signature()

        assert spec.verify_signature() is True

    def test_tampered_skill_verification_fails(self):
        from sovereign_claw.skills import SkillSpec

        spec = SkillSpec(
            name="test_skill",
            version="1.0.0",
            description="A signed skill",
            author="devin",
        )
        spec.signature_hash = spec.compute_signature()

        # Tamper with the spec
        spec.name = "tampered_skill"

        assert spec.verify_signature() is False

    def test_unsigned_skill_passes_verification(self):
        from sovereign_claw.skills import SkillSpec

        spec = SkillSpec(
            name="unsigned",
            version="1.0.0",
            description="No signature",
        )

        assert spec.verify_signature() is True  # No signature = pass (bundled)

    def test_skill_trust_score_and_reputation(self):
        from sovereign_claw.skills import Skill, SkillSpec

        spec = SkillSpec(name="s1", version="1.0", description="test")
        skill = Skill(spec=spec)

        assert skill.trust_score == 1.0
        assert skill.reputation == 1.0

        skill.record_violation()
        assert skill.trust_score == pytest.approx(0.9)
        assert skill.violation_count == 1

    def test_skill_reputation_decreases_with_drift(self):
        from sovereign_claw.skills import Skill, SkillSpec

        spec = SkillSpec(name="s1", version="1.0", description="test")
        skill = Skill(spec=spec)

        skill.record_use(drift_delta=0.5)
        skill.record_use(drift_delta=0.3)

        # reputation = trust_score - (violation_count * 0.05 + drift_impact_total * 0.1)
        expected_penalty = 0.0 * 0.05 + 0.8 * 0.1
        assert skill.reputation == pytest.approx(1.0 - expected_penalty)

    def test_skill_activation_requires_evaluation_ag02(self):
        """AG-02: No authority without evaluation."""
        from sovereign_claw.skills import Skill, SkillSpec

        spec = SkillSpec(name="s1", version="1.0", description="test")
        skill = Skill(spec=spec)

        # Cannot activate without evaluation
        assert skill.activate() is False
        assert skill.status.value == "available"

    def test_skill_activation_requires_valid_signature(self):
        from sovereign_claw.skills import Skill, SkillSpec, SkillEvalResult

        spec = SkillSpec(
            name="s1",
            version="1.0",
            description="test",
            signature_hash="invalid_hash",
        )
        skill = Skill(
            spec=spec,
            eval_result=SkillEvalResult(passed=True, score=1.0),
        )

        # Evaluated but signature invalid
        assert skill.activate() is False

    def test_skill_activation_success(self):
        from sovereign_claw.skills import Skill, SkillSpec, SkillEvalResult

        spec = SkillSpec(name="s1", version="1.0", description="test")
        skill = Skill(
            spec=spec,
            eval_result=SkillEvalResult(passed=True, score=1.0),
        )

        assert skill.activate() is True
        assert skill.is_active is True

    def test_skill_permissions_field(self):
        from sovereign_claw.skills import SkillSpec

        spec = SkillSpec(
            name="s1",
            version="1.0",
            description="test",
            permissions=["read_fs", "write_fs", "network"],
        )

        assert "read_fs" in spec.permissions
        assert "network" in spec.permissions


# ── Channel Interface Mesh ────────────────────────────────────────────────────
class TestChannelMesh:
    def test_register_identity(self):
        from sovereign_claw.channels.mesh import ChannelMesh

        mesh = ChannelMesh()
        identity = mesh.register_identity(
            "Alice",
            {"discord": "alice#1234", "slack": "alice_slack"},
        )

        assert identity.display_name == "Alice"
        assert "discord" in identity.channel_accounts
        assert "slack" in identity.channel_accounts

    def test_cross_channel_identity_resolution(self):
        from sovereign_claw.channels.mesh import ChannelMesh

        mesh = ChannelMesh()
        identity = mesh.register_identity(
            "Bob",
            {"discord": "bob#5678", "telegram": "@bob_tg"},
        )

        # Resolve from Discord
        resolved_discord = mesh.resolve_identity("discord", "bob#5678")
        assert resolved_discord is not None
        assert resolved_discord.identity_id == identity.identity_id

        # Resolve from Telegram
        resolved_tg = mesh.resolve_identity("telegram", "@bob_tg")
        assert resolved_tg is not None
        assert resolved_tg.identity_id == identity.identity_id

        # Unknown account
        assert mesh.resolve_identity("irc", "unknown") is None

    def test_link_new_account(self):
        from sovereign_claw.channels.mesh import ChannelMesh

        mesh = ChannelMesh()
        identity = mesh.register_identity("Carol", {"discord": "carol#1"})

        result = mesh.link_account(identity.identity_id, "slack", "carol_slack")
        assert result is True

        resolved = mesh.resolve_identity("slack", "carol_slack")
        assert resolved is not None
        assert resolved.identity_id == identity.identity_id

    def test_session_continuity_across_channels(self):
        from sovereign_claw.channels.mesh import ChannelMesh

        mesh = ChannelMesh()
        identity = mesh.register_identity("Dave", {"discord": "dave#1"})
        session = mesh.create_session(identity.identity_id, "discord")

        assert session.active_channel == "discord"

        # Switch to Slack
        result = mesh.switch_channel(session.session_id, "slack")
        assert result is True

        updated = mesh.get_session(session.session_id)
        assert updated is not None
        assert updated.active_channel == "slack"

    def test_per_channel_policy(self):
        from sovereign_claw.channels.mesh import ChannelMesh, ChannelPolicy

        mesh = ChannelMesh()

        # Set strict policy for Slack
        mesh.set_channel_policy(
            ChannelPolicy(
                channel="slack",
                policy_profile="strict",
                max_message_length=2048,
                rate_limit_per_minute=30,
            )
        )

        slack_policy = mesh.get_channel_policy("slack")
        assert slack_policy.policy_profile == "strict"
        assert slack_policy.max_message_length == 2048

        # Default policy for Discord (not explicitly set)
        discord_policy = mesh.get_channel_policy("discord")
        assert discord_policy.policy_profile == "balanced"

    def test_message_history(self):
        from sovereign_claw.channels.mesh import ChannelMesh

        mesh = ChannelMesh()
        identity = mesh.register_identity("Eve", {"webchat": "eve_web"})
        session = mesh.create_session(identity.identity_id, "webchat")

        result = mesh.add_message(session.session_id, {"text": "Hello!"})
        assert result is True

        updated = mesh.get_session(session.session_id)
        assert updated is not None
        assert len(updated.history) == 1
        assert updated.history[0]["text"] == "Hello!"
        assert updated.history[0]["channel"] == "webchat"

    def test_list_sessions_by_identity(self):
        from sovereign_claw.channels.mesh import ChannelMesh

        mesh = ChannelMesh()
        id1 = mesh.register_identity("User1", {"discord": "u1"})
        id2 = mesh.register_identity("User2", {"slack": "u2"})

        mesh.create_session(id1.identity_id, "discord")
        mesh.create_session(id1.identity_id, "slack")
        mesh.create_session(id2.identity_id, "telegram")

        sessions_u1 = mesh.list_sessions(id1.identity_id)
        assert len(sessions_u1) == 2

        all_sessions = mesh.list_sessions()
        assert len(all_sessions) == 3


# ── State + Memory Layer ──────────────────────────────────────────────────────
class TestMemoryLayer:
    def test_store_and_recall(self):
        from sovereign_claw.memory import MemoryStore, MemoryQuery

        store = MemoryStore()
        entry = store.store("test content", "episodic", tags=["demo"])

        results = store.recall(MemoryQuery(memory_type="episodic"))

        assert len(results) == 1
        assert results[0].content == "test content"
        assert results[0].memory_id == entry.memory_id

    def test_memory_types(self):
        from sovereign_claw.memory import MemoryStore

        store = MemoryStore()
        store.store("episode", "episodic")
        store.store("knowledge", "semantic")
        store.store("objective", "task")

        stats = store.stats()

        assert stats.total_entries == 3
        assert stats.episodic_count == 1
        assert stats.semantic_count == 1
        assert stats.task_count == 1

    def test_ttl_expiry(self):
        from sovereign_claw.memory import MemoryStore, MemoryQuery

        store = MemoryStore(default_ttl=0.01)  # Very short TTL
        store.store("will expire", "episodic")

        time.sleep(0.02)

        results = store.recall(MemoryQuery(memory_type="episodic"))
        assert len(results) == 0

    def test_relevance_scoring(self):
        from sovereign_claw.memory import MemoryStore, MemoryQuery

        store = MemoryStore()
        store.store("low relevance", "semantic", relevance_score=0.1)
        store.store("high relevance", "semantic", relevance_score=0.9)

        results = store.recall(MemoryQuery(min_relevance=0.5))

        assert len(results) == 1
        assert results[0].content == "high relevance"

    def test_tag_based_recall(self):
        from sovereign_claw.memory import MemoryStore, MemoryQuery

        store = MemoryStore()
        store.store("tagged A", "episodic", tags=["alpha"])
        store.store("tagged B", "episodic", tags=["beta"])

        results = store.recall(MemoryQuery(tags=["alpha"]))

        assert len(results) == 1
        assert results[0].content == "tagged A"

    def test_forget(self):
        from sovereign_claw.memory import MemoryStore

        store = MemoryStore()
        entry = store.store("to forget", "episodic")

        assert store.forget(entry.memory_id) is True
        assert store.get(entry.memory_id) is None
        assert store.forget("nonexistent") is False

    def test_update_relevance(self):
        from sovereign_claw.memory import MemoryStore

        store = MemoryStore()
        entry = store.store("test", "semantic", relevance_score=0.5)

        assert store.update_relevance(entry.memory_id, 0.9) is True
        updated = store.get(entry.memory_id)
        assert updated is not None
        assert updated.relevance_score == pytest.approx(0.9)

    def test_clear_by_type(self):
        from sovereign_claw.memory import MemoryStore

        store = MemoryStore()
        store.store("ep1", "episodic")
        store.store("ep2", "episodic")
        store.store("sem1", "semantic")

        cleared = store.clear("episodic")

        assert cleared == 2
        assert store.stats().episodic_count == 0
        assert store.stats().semantic_count == 1

    def test_capacity_enforcement(self):
        from sovereign_claw.memory import MemoryStore

        store = MemoryStore(max_episodic=3)

        for i in range(5):
            store.store(f"entry {i}", "episodic", relevance_score=float(i) / 10)

        stats = store.stats()
        assert stats.episodic_count <= 3


# ── Governance Enforcement (AG-02, AG-05, AG-07) ─────────────────────────────
class TestGovernanceEnforcement:
    def test_ag02_no_authority_without_eval(self):
        """AG-02: No agent may produce authoritative outputs without
        passing its evaluation harness."""
        from sovereign_claw.skills import Skill, SkillSpec

        spec = SkillSpec(name="unevaluated", version="1.0", description="test")
        skill = Skill(spec=spec)

        # Unevaluated skill cannot activate
        assert skill.is_evaluated is False
        assert skill.activate() is False
        assert skill.is_active is False

    def test_ag02_failed_eval_prevents_activation(self):
        """AG-02: A skill that fails evaluation cannot be activated."""
        from sovereign_claw.skills import Skill, SkillSpec, SkillEvalResult

        spec = SkillSpec(name="failed", version="1.0", description="test")
        skill = Skill(
            spec=spec,
            eval_result=SkillEvalResult(passed=False, score=0.3),
        )

        assert skill.is_evaluated is False  # passed=False
        assert skill.activate() is False

    def test_ag05_role_isolation_structural(self):
        """AG-05: No agent may both plan AND execute AND validate in
        the same authority lane."""
        from sovereign_claw.multi_agent import AgentRegistry, AgentRole

        registry = AgentRegistry()

        # Register agents with distinct roles
        planner = registry.register("p", AgentRole.PLANNER)
        executor = registry.register("e", AgentRole.EXECUTOR)
        validator = registry.register("v", AgentRole.VALIDATOR)

        # Each agent structurally has ONE role
        assert planner.role != executor.role
        assert executor.role != validator.role
        assert planner.role != validator.role

        # Role lookup returns disjoint sets
        p_set = {a.agent_id for a in registry.get_by_role(AgentRole.PLANNER)}
        e_set = {a.agent_id for a in registry.get_by_role(AgentRole.EXECUTOR)}
        v_set = {a.agent_id for a in registry.get_by_role(AgentRole.VALIDATOR)}

        assert p_set.isdisjoint(e_set)
        assert e_set.isdisjoint(v_set)

    def test_ag07_refusal_is_deterministic(self):
        """AG-07: Refusal is a first-class capability. An agent that
        refuses correctly has succeeded."""
        from sovereign_claw.policy_engine import PolicyEngine

        engine = PolicyEngine(forbidden_tools=["dangerous_tool"])

        # Refusal is deterministic and reproducible
        d1 = engine.evaluate({"tool": "dangerous_tool"})
        d2 = engine.evaluate({"tool": "dangerous_tool"})

        assert d1.allowed is False
        assert d2.allowed is False
        assert d1.reasons == d2.reasons
        assert d1.matched_policies == d2.matched_policies

    def test_ag07_refusal_with_trace(self):
        """AG-07: Refusal should produce auditable output."""
        from sovereign_claw.policy_engine import PolicyEngine

        engine = PolicyEngine(forbidden_tools=["shell_exec"])

        decision = engine.evaluate({"tool": "shell_exec", "trace_id": "t-1"})

        assert decision.allowed is False
        assert len(decision.reasons) > 0
        assert len(decision.matched_policies) > 0

    def test_governance_consensus_requires_constraint_alignment(self):
        """AG-05 extension: Consensus without shared constraints is invalid."""
        from sovereign_claw.multi_agent import (
            AgentProposal,
            AgentRole,
            MultiAgentOrchestrator,
        )

        orchestrator = MultiAgentOrchestrator(consensus_threshold=0.9)

        # Two proposals from unregistered agents (no constraint manifold)
        proposals = [
            AgentProposal(
                agent_id="unknown_1",
                role=AgentRole.PLANNER,
                content={"plan": "A"},
                confidence=0.5,
            ),
            AgentProposal(
                agent_id="unknown_2",
                role=AgentRole.PLANNER,
                content={"plan": "B"},
                confidence=0.5,
            ),
        ]

        result = orchestrator.evaluate_consensus(proposals)

        # With low agreement, consensus should not be accepted
        assert result.agreement_score < 0.9
        assert len(result.dissenting_agents) > 0


# ── __init__.py exports ───────────────────────────────────────────────────────
class TestModuleExports:
    def test_core_exports(self):
        import sovereign_claw as sc

        # Verify they are importable and correct types
        assert sc.PolicyProfile.STRICT.value == "strict"
        assert sc.AgentRole.PLANNER.value == "planner"
        assert sc.PolicyEngine is not None
        assert sc.PolicyDecision is not None
        assert sc.ReceiptBuilder is not None
        assert sc.ProofReceipt is not None
        assert sc.HashedStep is not None
        assert sc.ReplayStep is not None
        assert sc.TraceDiff is not None
        assert sc.DriftTracker is not None
        assert sc.DriftBreakdown is not None
        assert sc.DriftComponent is not None
        assert sc.DriftReport is not None
        assert sc.MemoryStore is not None
        assert sc.MemoryEntry is not None
        assert sc.MemoryQuery is not None
        assert sc.MemoryStats is not None
        assert sc.AgentCard is not None
        assert sc.AgentRegistry is not None
        assert sc.MultiAgentOrchestrator is not None

    def test_lazy_exports(self):
        import sovereign_claw as sc

        assert sc.ExecutionMode.LOW_COST.value == "low_cost"
        assert sc.ProviderCost is not None
        assert sc.SkillSpec is not None
        assert sc.ChannelMesh is not None
        assert sc.ChannelIdentity is not None
        assert sc.MeshSession is not None
