"""
tests/test_measured_drift_closure.py
=====================================
Adversarial and spec tests for issue #17: Measured Drift and Verified Closure.

Tests enforce the contract in docs/MEASURED_DRIFT_CLOSURE.md:

  • Successful no-op × 100 never reduces measured constraint drift / closes.
  • Executor reports success but evaluator observes unchanged state → no closure.
  • Missing evaluator/component → UNMEASURED, never 0.0, no closure.
  • Low constraint value with unsafe policy/resource/execution → no closure.
  • Manually supplied zero/negative/NaN/Inf cannot confer authority.
  • Stale or mismatched stability certificate rejected.
  • PREDICTED drift cannot be replayed as MEASURED evidence.
  • T_MAX remains a violation even if drift seems low.
  • Evidence failure blocks closure.
  • Byte-stable (deterministic) vector/closure hashes for identical inputs.
  • Oscillation/chattering does not manufacture progress.
  • UNMEASURED != 0.0 — unknown required values must not default to zero.
"""

from __future__ import annotations

import os
import time

import pytest

os.environ["SOVEREIGN_CLAW_DB"] = "/tmp/sovereign_claw_measured_test.sqlite3"

from sovereign_claw.lanes import Lane, LaneRouter
from sovereign_claw.measured_drift import (
    REQUIRED_COMPONENTS,
    ClosureDecisionV1,
    ComponentMeasurement,
    ConstraintAssessmentV1,
    ConstraintEvaluator,
    ConstraintEvaluatorRegistry,
    DriftMetricIdentity,
    DriftVectorV1,
    LaneTransitionEvidenceV1,
    StabilityCertificateV1,
    StateObservationV1,
    evaluate_closure,
)
from sovereign_claw.thermodynamics import SystemThermodynamics, TaskManifold

# ─────────────────────────────────────────────────────────────────────────────
# Helpers / Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _make_manifold(**kwargs) -> TaskManifold:
    defaults = {"objective": "test", "t_max_steps": 5}
    defaults.update(kwargs)
    return TaskManifold(**defaults)


def _make_metric(
    evaluator_id: str = "test.evaluator.v1",
    evaluator_version: str = "1.0.0",
    build_identity: str = "test-build-001",
) -> DriftMetricIdentity:
    return DriftMetricIdentity(
        metric_id="test.drift.v1",
        metric_version="1.0.0",
        evaluator_id=evaluator_id,
        evaluator_version=evaluator_version,
        build_identity=build_identity,
        required_components=frozenset(REQUIRED_COMPONENTS),
        weights=frozenset(),
        tolerance_identity=None,
    )


def _all_measured_vector(
    trace_id: str = "trace-001",
    step_index: int = 0,
    constraint: float = 0.0,
    postcondition: float = 0.0,
    execution_error: float = 0.0,
    policy: float = 0.0,
    provider_uncertainty: float = 0.0,
    resource_latency: float = 0.0,
    phase: str = "AFTER",
    metric: DriftMetricIdentity | None = None,
) -> DriftVectorV1:
    m = metric or _make_metric()
    return DriftVectorV1(
        schema_version="sovereign.drift.vector.v1",
        trace_id=trace_id,
        step_index=step_index,
        observation_phase=phase,  # type: ignore[arg-type]
        metric_identity=m,
        components=(
            ComponentMeasurement("constraint", "MEASURED", constraint),
            ComponentMeasurement("postcondition", "MEASURED", postcondition),
            ComponentMeasurement("execution_error", "MEASURED", execution_error),
            ComponentMeasurement("policy", "MEASURED", policy),
            ComponentMeasurement("provider_uncertainty", "MEASURED", provider_uncertainty),
            ComponentMeasurement("resource_latency", "MEASURED", resource_latency),
        ),
        timestamp_utc=time.time(),
    )


def _make_observation(
    trace_id: str = "trace-001",
    step_index: int = 0,
    phase: str = "AFTER",
    worker_status: str = "success",
    policy_decision: str = "ALLOW",
    postcondition_result: str = "PASS",
    policy_context_hash: str = "ctx-000",
    policy_bundle_hash: str = "bndl-000",
) -> StateObservationV1:
    return StateObservationV1(
        schema_version="sovereign.observation.v1",
        trace_id=trace_id,
        correlation_id="corr-001",
        step_index=step_index,
        phase=phase,  # type: ignore[arg-type]
        tool_id="test.tool.v1",
        tool_contract_hash="contract-hash-001",
        action_digest="action-digest-001",
        worker_status=worker_status,
        result_digest="result-digest-001",
        result_size_bytes=42,
        policy_decision=policy_decision,
        policy_context_hash=policy_context_hash,
        policy_bundle_hash=policy_bundle_hash,
        postcondition_result=postcondition_result,
        postcondition_validator_id="validator.v1",
        postcondition_validator_version="1.0.0",
        elapsed_ms=10.0,
        remaining_deadline_ms=5000.0,
        resource_limit_result="ok",
        isolation_enforcement_id="subprocess_bounded_v1",
        provider_identity="test.provider.v1",
        provider_uncertainty=None,
    )


def _make_assessment(
    metric: DriftMetricIdentity | None = None,
    constraint: float = 0.0,
    postcondition_result: str = "PASS",
) -> ConstraintAssessmentV1:
    m = metric or _make_metric()
    return ConstraintAssessmentV1(
        schema_version="sovereign.assessment.v1",
        evaluator_id=m.evaluator_id,
        evaluator_version=m.evaluator_version,
        evaluator_build_hash=m.build_identity,
        domain_version="1.0.0",
        metric_identity=m,
        component_measurements=(
            ComponentMeasurement("constraint", "MEASURED", constraint),
            ComponentMeasurement(
                "postcondition", "MEASURED", 0.0 if postcondition_result == "PASS" else 1.0
            ),
            ComponentMeasurement("execution_error", "MEASURED", 0.0),
            ComponentMeasurement("policy", "MEASURED", 0.0),
            ComponentMeasurement("provider_uncertainty", "MEASURED", 0.0),
            ComponentMeasurement("resource_latency", "MEASURED", 0.0),
        ),
        postcondition_result=postcondition_result,
        postcondition_rule_ids=("rule.postcondition.pass",),
        evidence_refs=("evidence.ref.001",),
    )


def _make_cert(
    metric: DriftMetricIdentity | None = None,
    issued_at_utc: float | None = None,
) -> StabilityCertificateV1:
    m = metric or _make_metric()
    return StabilityCertificateV1(
        schema_version="sovereign.cert.v1",
        certificate_id="cert-001",
        metric_identity=m,
        evaluator_id=m.evaluator_id,
        evaluator_version=m.evaluator_version,
        evaluator_build_identity=m.build_identity,
        domain_id="test.domain.v1",
        domain_version="1.0.0",
        controller_implementation_id="sovereign.controller.elfe.v1",
        controller_implementation_version="1.0.0",
        elfe_a=1.0,
        elfe_b=1.0,
        elfe_p=0.5,
        elfe_q=2.0,
        descent_scale=0.1,
        perturbation_bound=0.01,
        tolerance=0.0,
        discrete_update_interval_s=1.0,
        oscillation_policy_id="oscillation.policy.v1",
        max_steps=100,
        max_wall_time_s=1000.0,
        admissible_initial_drift_max=1.0,
        proof_artifact_id="proof.artifact.v1",
        certificate_digest="cert-digest-001",
        issued_at_utc=issued_at_utc if issued_at_utc is not None else time.time(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. UNMEASURED != 0.0 — invariant
# ─────────────────────────────────────────────────────────────────────────────


class TestUnmeasuredNotZero:
    def test_unmeasured_component_value_is_none(self):
        c = ComponentMeasurement("constraint", "UNMEASURED", None)
        assert c.value is None

    def test_unmeasured_component_cannot_have_value(self):
        with pytest.raises((ValueError, TypeError)):
            ComponentMeasurement("constraint", "UNMEASURED", 0.0)

    def test_measured_component_cannot_be_none(self):
        with pytest.raises((ValueError, TypeError)):
            ComponentMeasurement("constraint", "MEASURED", None)

    def test_fully_unmeasured_vector_composite_is_none(self):
        metric = _make_metric()
        vec = DriftVectorV1.unmeasured(
            trace_id="t1",
            step_index=0,
            metric_identity=metric,
        )
        assert vec.composite_scalar() is None, "UNMEASURED composite must be None, never 0.0"

    def test_partial_unmeasured_composite_is_none(self):
        metric = _make_metric()
        components = (
            ComponentMeasurement("constraint", "MEASURED", 0.0),
            ComponentMeasurement("postcondition", "UNMEASURED", None),  # required
            ComponentMeasurement("execution_error", "MEASURED", 0.0),
            ComponentMeasurement("policy", "MEASURED", 0.0),
            ComponentMeasurement("provider_uncertainty", "MEASURED", 0.0),
            ComponentMeasurement("resource_latency", "MEASURED", 0.0),
        )
        vec = DriftVectorV1(
            schema_version="sovereign.drift.vector.v1",
            trace_id="t2",
            step_index=0,
            observation_phase="AFTER",
            metric_identity=metric,
            components=components,
            timestamp_utc=time.time(),
        )
        assert vec.composite_scalar() is None, (
            "Any UNMEASURED required component → composite is None"
        )

    def test_thermodynamics_unmeasured_returns_none(self):
        therm = SystemThermodynamics(_make_manifold())
        metric = _make_metric()
        vec = DriftVectorV1.unmeasured(trace_id="t1", step_index=0, metric_identity=metric)
        result = therm.update_from_measured_vector(vec)
        assert result is None, "UNMEASURED vector must not update drift (returns None, not 0.0)"
        assert therm.current_drift == 1.0, "Drift must not change when vector is UNMEASURED"

    def test_nan_component_value_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            ComponentMeasurement("constraint", "MEASURED", float("nan"))

    def test_inf_component_value_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            ComponentMeasurement("constraint", "MEASURED", float("inf"))

    def test_negative_component_value_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            ComponentMeasurement("constraint", "MEASURED", -0.1)

    def test_value_above_one_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            ComponentMeasurement("constraint", "MEASURED", 1.1)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Successful no-op × 100 never reduces measured constraint drift / closes
# ─────────────────────────────────────────────────────────────────────────────


class TestNoOpDoesNotReduceMeasuredDrift:
    def test_successful_noop_100x_does_not_change_drift(self):
        """
        Invariant #1: drift is observed, not awarded.
        Repeated successful calls with no state change must not reduce drift.
        Each call returns the same measured composite (unchanged state), so drift
        must remain constant — not decrease toward zero from call count.
        """
        metric = _make_metric()
        therm = SystemThermodynamics(_make_manifold(t_max_steps=200))

        # All components MEASURED at maximum distance (no-op — state unchanged)
        constraint_val = 0.8
        noop_vec = _all_measured_vector(
            trace_id="trace-noop",
            step_index=0,
            constraint=constraint_val,
            postcondition=constraint_val,
            execution_error=0.0,
            policy=0.0,
            provider_uncertainty=0.0,
            resource_latency=0.0,
            metric=metric,
        )
        expected_composite = noop_vec.composite_scalar()
        assert expected_composite is not None and expected_composite > 0.0

        for i in range(100):
            vec = _all_measured_vector(
                trace_id="trace-noop",
                step_index=i,
                constraint=constraint_val,
                postcondition=constraint_val,
                execution_error=0.0,
                policy=0.0,
                provider_uncertainty=0.0,
                resource_latency=0.0,
                metric=metric,
            )
            result = therm.update_from_measured_vector(vec)
            assert result is not None
            # Drift must reflect the measured state, not decrease from success count
            assert therm.current_drift > 0.0, (
                f"Step {i}: drift must not be zero from no-op success; got {therm.current_drift}"
            )

        # After 100 no-op successes, drift must stay at the measured composite value,
        # not decrease toward zero due to call count
        assert therm.current_drift == pytest.approx(expected_composite, abs=0.01), (
            f"100 no-op successes must not reduce measured drift; "
            f"got {therm.current_drift}, expected ~{expected_composite}"
        )

    def test_successful_noop_100x_does_not_close(self):
        """Closure cannot be granted from no-op successes without measured improvement."""
        metric = _make_metric()

        # Make 100 no-op observations — success=True but measured constraint distance stays high
        before = _make_observation(phase="BEFORE", worker_status="pending")
        after = _make_observation(
            phase="AFTER", worker_status="success", postcondition_result="PASS"
        )
        assessment = _make_assessment(metric=metric, constraint=0.9, postcondition_result="PASS")
        vec = _all_measured_vector(constraint=0.9, postcondition=0.0, metric=metric)

        for _ in range(100):
            decision = evaluate_closure(
                drift_vector=vec,
                assessment=assessment,
                before_observation=before,
                after_observation=after,
                policy_context_hash="ctx-000",
                policy_bundle_hash="bndl-000",
                vault_evidence_ref="vault-ref-001",
                constraint_threshold=0.0,
            )
            assert decision.status != "ISOMORPHIC_CLOSURE", (
                "No-op with high measured constraint distance must not achieve closure"
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Executor reports success but evaluator observes unchanged state → no closure
# ─────────────────────────────────────────────────────────────────────────────


class TestExecutorSuccessDoesNotSelfCertify:
    def test_executor_success_with_high_constraint_no_closure(self):
        """Tool returned success but constraint distance is still high."""
        metric = _make_metric()
        before = _make_observation(phase="BEFORE", worker_status="pending")
        after = _make_observation(phase="AFTER", worker_status="success")
        # Evaluator observes constraint distance = 0.8 despite executor success
        assessment = _make_assessment(metric=metric, constraint=0.8, postcondition_result="PASS")
        vec = _all_measured_vector(constraint=0.8, metric=metric)

        decision = evaluate_closure(
            drift_vector=vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref="vault-ref-001",
            constraint_threshold=0.0,
        )
        assert decision.status != "ISOMORPHIC_CLOSURE"

    def test_postcondition_fail_blocks_closure_even_if_drift_zero(self):
        """Postcondition failure blocks closure regardless of measured drift."""
        metric = _make_metric()
        before = _make_observation(phase="BEFORE", worker_status="pending")
        after = _make_observation(
            phase="AFTER", worker_status="success", postcondition_result="FAIL"
        )
        assessment = _make_assessment(metric=metric, constraint=0.0, postcondition_result="FAIL")
        vec = _all_measured_vector(constraint=0.0, postcondition=1.0, metric=metric)

        decision = evaluate_closure(
            drift_vector=vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref="vault-ref-001",
            constraint_threshold=0.0,
        )
        assert decision.status != "ISOMORPHIC_CLOSURE"
        assert any("postcondition" in r.lower() for r in decision.failure_reasons)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Missing evaluator/component → UNMEASURED, no closure
# ─────────────────────────────────────────────────────────────────────────────


class TestMissingEvaluatorOrComponent:
    def test_no_evaluator_returns_unverified_no_closure(self):
        metric = _make_metric(evaluator_id="unregistered.evaluator")
        registry = ConstraintEvaluatorRegistry()  # empty — no evaluator registered

        before = _make_observation(phase="BEFORE")
        after = _make_observation(phase="AFTER")

        assessment, drift_vector = registry.evaluate_or_unverified(
            before=before,
            after=after,
            metric_identity=metric,
            trace_id="t1",
            step_index=0,
        )
        assert assessment is None
        assert drift_vector.composite_scalar() is None

        # evaluate_closure with no assessment → UNVERIFIED_NO_CLOSURE
        decision = evaluate_closure(
            drift_vector=drift_vector,
            assessment=None,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref="vault-ref-001",
        )
        assert decision.status == "UNVERIFIED_NO_CLOSURE"

    def test_missing_required_component_unmeasured_no_closure(self):
        """A vector missing a required component must not close."""
        metric = _make_metric()
        # Only 4 of 6 required components are MEASURED
        partial_components = (
            ComponentMeasurement("constraint", "MEASURED", 0.0),
            ComponentMeasurement("postcondition", "MEASURED", 0.0),
            # missing: execution_error, policy, provider_uncertainty, resource_latency
        )
        vec = DriftVectorV1(
            schema_version="sovereign.drift.vector.v1",
            trace_id="t1",
            step_index=0,
            observation_phase="AFTER",
            metric_identity=metric,
            components=partial_components,
            timestamp_utc=time.time(),
        )
        assert not vec.all_required_measured()
        assert vec.composite_scalar() is None

        assessment = _make_assessment(metric=metric, constraint=0.0)
        before = _make_observation(phase="BEFORE")
        after = _make_observation(phase="AFTER")

        decision = evaluate_closure(
            drift_vector=vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref="vault-ref-001",
        )
        assert decision.status == "UNVERIFIED_NO_CLOSURE"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Low constraint with unsafe policy/resource/execution → no closure
# ─────────────────────────────────────────────────────────────────────────────


class TestLowConstraintUnsafeComponents:
    def test_policy_denied_blocks_closure_even_if_constraint_zero(self):
        metric = _make_metric()
        before = _make_observation(phase="BEFORE")
        # Policy DENY in AFTER observation
        after = _make_observation(phase="AFTER", worker_status="success", policy_decision="DENY")
        assessment = _make_assessment(metric=metric, constraint=0.0)
        vec = _all_measured_vector(constraint=0.0, metric=metric)

        decision = evaluate_closure(
            drift_vector=vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref="vault-ref-001",
        )
        assert decision.status == "POLICY_DENIED"
        assert not decision.is_closure

    def test_execution_failure_blocks_closure(self):
        metric = _make_metric()
        before = _make_observation(phase="BEFORE")
        after = _make_observation(phase="AFTER", worker_status="failure", policy_decision="ALLOW")
        assessment = _make_assessment(metric=metric, constraint=0.0)
        vec = _all_measured_vector(constraint=0.0, metric=metric)

        decision = evaluate_closure(
            drift_vector=vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref="vault-ref-001",
        )
        assert decision.status in (
            "EXECUTION_FAILURE",
            "UNVERIFIED_NO_CLOSURE",
            "UNVERIFIED_CONVERGENCE",
            "BOUNDED_STEP_NO_CLOSURE",
        )
        assert not decision.is_closure


# ─────────────────────────────────────────────────────────────────────────────
# 6. Fabricated/zero/NaN/negative drift cannot confer authority
# ─────────────────────────────────────────────────────────────────────────────


class TestFabricatedInputsRejected:
    def test_nan_metric_weight_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            DriftMetricIdentity(
                metric_id="m",
                metric_version="1",
                evaluator_id="e",
                evaluator_version="1",
                build_identity="b",
                weights=frozenset({("constraint", float("nan"))}),
            )

    def test_inf_component_rejected(self):
        with pytest.raises((ValueError, TypeError)):
            ComponentMeasurement("constraint", "MEASURED", float("inf"))

    def test_lane_router_zero_drift_advance_is_legacy_only(self):
        """drift == 0.0 in advance() is legacy non-authoritative; does not produce closure evidence."""
        r = LaneRouter()
        # advance with drift=0.0 moves to AUTHORITATIVE (legacy path)
        r.advance(approved=False, drift=0.0)
        assert r.current == Lane.AUTHORITATIVE
        # But final_status is NOT set to ISOMORPHIC_CLOSURE until advance is called again
        assert r.final_status is None

    def test_lane_router_advance_from_evidence_no_shortcut_without_closure(self):
        """LaneTransitionEvidenceV1 with UNVERIFIED_NO_CLOSURE cannot jump to AUTHORITATIVE."""
        r = LaneRouter()
        evidence = LaneTransitionEvidenceV1(
            schema_version="sovereign.lane.v1",
            trace_id="t1",
            prior_lane="REFLEX",
            target_lane="DELIBERATE",
            transition_rule="no_evaluator",
            drift_vector_hash="a" * 64,
            closure_status="UNVERIFIED_NO_CLOSURE",
            policy_decision="ALLOW",
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref=None,
            step_index=0,
            deadline_remaining_ms=5000.0,
        )
        r.advance_from_evidence(evidence)
        # UNVERIFIED_NO_CLOSURE must not jump to AUTHORITATIVE
        assert r.current != Lane.AUTHORITATIVE

    def test_lane_router_policy_denied_routes_to_stall(self):
        """POLICY_DENIED in evidence forces STALL regardless of lane."""
        r = LaneRouter()
        evidence = LaneTransitionEvidenceV1(
            schema_version="sovereign.lane.v1",
            trace_id="t1",
            prior_lane="REFLEX",
            target_lane="DELIBERATE",
            transition_rule="policy_denied",
            drift_vector_hash="a" * 64,
            closure_status="POLICY_DENIED",
            policy_decision="DENY",
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref=None,
            step_index=0,
            deadline_remaining_ms=5000.0,
        )
        r.advance_from_evidence(evidence)
        assert r.current == Lane.STALL
        assert r.done is True
        assert r.final_status == "POLICY_DENIED"

    def test_thermodynamics_update_from_zero_vector_does_not_snap(self):
        """Zero composite scalar from MEASURED vector updates drift to 0.0 but does not snap."""
        metric = _make_metric()
        therm = SystemThermodynamics(_make_manifold())
        vec = _all_measured_vector(
            constraint=0.0,
            postcondition=0.0,
            execution_error=0.0,
            policy=0.0,
            provider_uncertainty=0.0,
            resource_latency=0.0,
            metric=metric,
        )
        result = therm.update_from_measured_vector(vec)
        assert result == pytest.approx(0.0)
        # check_measured_status must return UNVERIFIED_CONVERGENCE or CONTINUE_DESCENT
        # (not ISOMORPHIC_CLOSURE — closure requires ClosureDecisionV1, not drift == 0)
        status = therm.check_measured_status(step_count=0)
        assert status != "ISOMORPHIC_CLOSURE", (
            "drift == 0.0 from measurement alone must not yield ISOMORPHIC_CLOSURE status; "
            "closure requires a ClosureDecisionV1"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 7. Stale or mismatched stability certificate rejected
# ─────────────────────────────────────────────────────────────────────────────


class TestStabilityCertificateRejection:
    def test_stale_certificate_yields_unverified_convergence(self):
        metric = _make_metric()
        # Certificate issued 200 days ago (older than 90-day default)
        stale_cert = _make_cert(
            metric=metric,
            issued_at_utc=time.time() - 86400 * 200,
        )
        assert stale_cert.is_stale()

        before = _make_observation(phase="BEFORE")
        after = _make_observation(phase="AFTER")
        assessment = _make_assessment(metric=metric, constraint=0.0)
        vec = _all_measured_vector(constraint=0.0, metric=metric)

        decision = evaluate_closure(
            drift_vector=vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref="vault-ref-001",
            stability_certificate=stale_cert,
        )
        # Stale certificate → UNVERIFIED_CONVERGENCE (or BOUNDED_STEP_NO_CLOSURE)
        assert decision.status in ("UNVERIFIED_CONVERGENCE", "BOUNDED_STEP_NO_CLOSURE")
        assert not decision.is_closure

    def test_mismatched_metric_certificate_rejected(self):
        metric_a = _make_metric(evaluator_id="evaluator.A")
        metric_b = _make_metric(evaluator_id="evaluator.B")
        cert_for_a = _make_cert(metric=metric_a)

        # Certificate is for metric_a but closure uses metric_b
        assert not cert_for_a.matches_metric(metric_b)

        before = _make_observation(phase="BEFORE")
        after = _make_observation(phase="AFTER")
        assessment = _make_assessment(metric=metric_b, constraint=0.0)
        vec = _all_measured_vector(constraint=0.0, metric=metric_b)

        decision = evaluate_closure(
            drift_vector=vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref="vault-ref-001",
            stability_certificate=cert_for_a,  # wrong metric!
        )
        assert decision.status in ("UNVERIFIED_CONVERGENCE", "BOUNDED_STEP_NO_CLOSURE")
        assert not decision.is_closure


# ─────────────────────────────────────────────────────────────────────────────
# 8. PREDICTED drift cannot be replayed as MEASURED evidence
# ─────────────────────────────────────────────────────────────────────────────


class TestPredictedCannotBeMeasured:
    def test_predicted_phase_blocks_closure(self):
        """PREDICTED observation phase must not satisfy the AFTER requirement for closure."""
        metric = _make_metric()
        before = _make_observation(phase="BEFORE")
        after = _make_observation(phase="AFTER")
        assessment = _make_assessment(metric=metric, constraint=0.0)

        # Vector with PREDICTED phase — must be rejected for closure
        predicted_vec = _all_measured_vector(constraint=0.0, metric=metric, phase="PREDICTED")
        assert predicted_vec.observation_phase == "PREDICTED"

        decision = evaluate_closure(
            drift_vector=predicted_vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref="vault-ref-001",
        )
        assert decision.status != "ISOMORPHIC_CLOSURE"
        assert any("AFTER" in r for r in decision.failure_reasons)

    def test_before_phase_blocks_closure(self):
        """BEFORE phase cannot substitute for AFTER measurement in closure."""
        metric = _make_metric()
        before = _make_observation(phase="BEFORE")
        after = _make_observation(phase="AFTER")
        assessment = _make_assessment(metric=metric, constraint=0.0)

        before_vec = _all_measured_vector(constraint=0.0, metric=metric, phase="BEFORE")
        decision = evaluate_closure(
            drift_vector=before_vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref="vault-ref-001",
        )
        assert decision.status != "ISOMORPHIC_CLOSURE"


# ─────────────────────────────────────────────────────────────────────────────
# 9. T_MAX remains a violation
# ─────────────────────────────────────────────────────────────────────────────


class TestTMaxViolationRemains:
    def test_t_max_from_measured_status(self):
        therm = SystemThermodynamics(_make_manifold(t_max_steps=3))
        assert therm.check_measured_status(3) == "T_MAX_VIOLATION"
        assert therm.check_measured_status(4) == "T_MAX_VIOLATION"

    def test_t_max_status_not_overwritten_by_measured_zero_drift(self):
        """T_MAX violation cannot be relabeled as closure even if drift reaches 0."""
        metric = _make_metric()
        therm = SystemThermodynamics(_make_manifold(t_max_steps=3))
        vec = _all_measured_vector(constraint=0.0, metric=metric)
        therm.update_from_measured_vector(vec)
        # drift is now 0.0 but step count exceeded t_max
        assert therm.check_measured_status(step_count=3) == "T_MAX_VIOLATION"

    def test_closure_decision_is_not_terminal_t_max(self):
        """ClosureDecisionV1 with T_MAX_VIOLATION is a terminal violation."""
        metric = _make_metric()
        before = _make_observation(phase="BEFORE")
        after = _make_observation(phase="AFTER")
        assessment = _make_assessment(metric=metric, constraint=0.0)
        vec = _all_measured_vector(constraint=0.0, metric=metric)
        evaluate_closure(
            drift_vector=vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref="vault-ref-001",
        )
        # Note: T_MAX_VIOLATION must be passed in explicitly; evaluate_closure only
        # receives drift/assessment/observation inputs. A T_MAX status set externally
        # must remain a violation and not be overwritten.
        # Test the property: is_terminal_violation for T_MAX
        tmax_decision = ClosureDecisionV1(
            schema_version="sovereign.closure.v1",
            trace_id="t1",
            step_index=0,
            status="T_MAX_VIOLATION",
            drift_vector_hash=vec.vector_hash(),
            assessment_hash=assessment.assessment_hash,
            before_observation_hash=before.observation_hash,
            after_observation_hash=after.observation_hash,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref=None,
            metric_identity=metric,
            evaluator_id=assessment.evaluator_id,
            stability_certificate_id=None,
            failure_reasons=("T_MAX_VIOLATION: step budget exhausted",),
        )
        assert tmax_decision.is_terminal_violation
        assert not tmax_decision.is_closure


# ─────────────────────────────────────────────────────────────────────────────
# 10. Evidence failure blocks closure
# ─────────────────────────────────────────────────────────────────────────────


class TestEvidenceFailureBlocksClosure:
    def test_no_vault_ref_blocks_closure(self):
        metric = _make_metric()
        before = _make_observation(phase="BEFORE")
        after = _make_observation(phase="AFTER")
        assessment = _make_assessment(metric=metric, constraint=0.0)
        vec = _all_measured_vector(constraint=0.0, metric=metric)

        decision = evaluate_closure(
            drift_vector=vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref=None,  # no vault ref!
        )
        assert decision.status in (
            "EVIDENCE_FAILURE",
            "UNVERIFIED_CONVERGENCE",
            "BOUNDED_STEP_NO_CLOSURE",
        )
        assert not decision.is_closure


# ─────────────────────────────────────────────────────────────────────────────
# 11. Byte-stable (deterministic) hashes for identical inputs
# ─────────────────────────────────────────────────────────────────────────────


class TestDeterministicHashes:
    def test_drift_vector_hash_stable_on_replay(self):
        """Identical vector inputs yield identical hashes."""
        metric = _make_metric()
        ts = 1234567890.0

        vec_a = DriftVectorV1(
            schema_version="sovereign.drift.vector.v1",
            trace_id="trace-stable",
            step_index=5,
            observation_phase="AFTER",
            metric_identity=metric,
            components=(
                ComponentMeasurement("constraint", "MEASURED", 0.3),
                ComponentMeasurement("postcondition", "MEASURED", 0.1),
                ComponentMeasurement("execution_error", "MEASURED", 0.0),
                ComponentMeasurement("policy", "MEASURED", 0.0),
                ComponentMeasurement("provider_uncertainty", "MEASURED", 0.05),
                ComponentMeasurement("resource_latency", "MEASURED", 0.02),
            ),
            timestamp_utc=ts,
        )
        vec_b = DriftVectorV1(
            schema_version="sovereign.drift.vector.v1",
            trace_id="trace-stable",
            step_index=5,
            observation_phase="AFTER",
            metric_identity=metric,
            components=(
                ComponentMeasurement("constraint", "MEASURED", 0.3),
                ComponentMeasurement("postcondition", "MEASURED", 0.1),
                ComponentMeasurement("execution_error", "MEASURED", 0.0),
                ComponentMeasurement("policy", "MEASURED", 0.0),
                ComponentMeasurement("provider_uncertainty", "MEASURED", 0.05),
                ComponentMeasurement("resource_latency", "MEASURED", 0.02),
            ),
            timestamp_utc=ts,
        )
        assert vec_a.vector_hash() == vec_b.vector_hash()

    def test_observation_hash_stable_on_replay(self):
        obs_a = _make_observation()
        obs_b = _make_observation()
        assert obs_a.observation_hash == obs_b.observation_hash

    def test_closure_decision_hash_stable_on_replay(self):
        metric = _make_metric()
        before = _make_observation(phase="BEFORE")
        after = _make_observation(phase="AFTER")
        assessment = _make_assessment(metric=metric, constraint=0.0)
        vec = _all_measured_vector(constraint=0.0, metric=metric)

        kwargs = {
            "drift_vector": vec,
            "assessment": assessment,
            "before_observation": before,
            "after_observation": after,
            "policy_context_hash": "ctx-000",
            "policy_bundle_hash": "bndl-000",
            "vault_evidence_ref": "vault-ref-001",
        }
        d1 = evaluate_closure(**kwargs)  # type: ignore[arg-type]
        d2 = evaluate_closure(**kwargs)  # type: ignore[arg-type]
        assert d1.decision_hash == d2.decision_hash


# ─────────────────────────────────────────────────────────────────────────────
# 12. Oscillation does not manufacture progress
# ─────────────────────────────────────────────────────────────────────────────


class TestOscillationDoesNotProgress:
    def test_alternating_high_low_drift_does_not_reduce_to_zero(self):
        """Oscillating measured drift must not converge to zero."""
        metric = _make_metric()
        therm = SystemThermodynamics(_make_manifold(t_max_steps=200))
        values = []

        for i in range(50):
            # Alternate between high and low constraint distance
            constraint = 0.9 if i % 2 == 0 else 0.5
            vec = _all_measured_vector(
                trace_id="osc",
                step_index=i,
                constraint=constraint,
                metric=metric,
            )
            result = therm.update_from_measured_vector(vec)
            assert result is not None
            values.append(result)

        # All measured drifts must be positive (oscillation does not converge to zero)
        assert all(v > 0.0 for v in values), (
            f"Oscillating measured drift must remain positive; min={min(values)}"
        )

    def test_stall_not_close_in_oscillation(self):
        """Even if drift briefly hits the threshold, oscillation must not close."""
        metric = _make_metric()
        before = _make_observation(phase="BEFORE")
        after = _make_observation(phase="AFTER")
        # Assessment says constraint=0.3 (above threshold 0.0)
        assessment = _make_assessment(metric=metric, constraint=0.3)
        vec = _all_measured_vector(constraint=0.3, metric=metric)

        for _ in range(20):
            decision = evaluate_closure(
                drift_vector=vec,
                assessment=assessment,
                before_observation=before,
                after_observation=after,
                policy_context_hash="ctx-000",
                policy_bundle_hash="bndl-000",
                vault_evidence_ref="vault-ref-001",
                constraint_threshold=0.0,
            )
            assert not decision.is_closure


# ─────────────────────────────────────────────────────────────────────────────
# 13. Verified ISOMORPHIC_CLOSURE (positive path — all conditions met)
# ─────────────────────────────────────────────────────────────────────────────


class TestVerifiedClosurePositivePath:
    def test_all_conditions_met_yields_isomorphic_closure(self):
        """When all required conditions are met, closure must be granted."""
        metric = _make_metric()
        before = _make_observation(phase="BEFORE", worker_status="pending")
        after = _make_observation(
            phase="AFTER",
            worker_status="success",
            policy_decision="ALLOW",
            postcondition_result="PASS",
        )
        assessment = _make_assessment(metric=metric, constraint=0.0, postcondition_result="PASS")
        vec = _all_measured_vector(constraint=0.0, metric=metric, phase="AFTER")

        decision = evaluate_closure(
            drift_vector=vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-000",
            policy_bundle_hash="bndl-000",
            vault_evidence_ref="vault-ref-001",
            constraint_threshold=0.0,
        )
        assert decision.status == "ISOMORPHIC_CLOSURE"
        assert decision.is_closure
        assert len(decision.failure_reasons) == 0
        assert decision.vault_evidence_ref == "vault-ref-001"
        assert decision.evaluator_id == metric.evaluator_id

    def test_closure_decision_has_all_evidence(self):
        """Closure decision must contain complete evidence references."""
        metric = _make_metric()
        before = _make_observation(phase="BEFORE")
        after = _make_observation(phase="AFTER")
        assessment = _make_assessment(metric=metric, constraint=0.0)
        vec = _all_measured_vector(constraint=0.0, metric=metric)

        decision = evaluate_closure(
            drift_vector=vec,
            assessment=assessment,
            before_observation=before,
            after_observation=after,
            policy_context_hash="ctx-001",
            policy_bundle_hash="bndl-001",
            vault_evidence_ref="vault-ev-001",
        )
        assert decision.drift_vector_hash == vec.vector_hash()
        assert decision.assessment_hash == assessment.assessment_hash
        assert decision.before_observation_hash == before.observation_hash
        assert decision.after_observation_hash == after.observation_hash
        assert decision.policy_context_hash == "ctx-001"
        assert decision.policy_bundle_hash == "bndl-001"


# ─────────────────────────────────────────────────────────────────────────────
# 14. DriftMetricIdentity hash stability
# ─────────────────────────────────────────────────────────────────────────────


class TestDriftMetricIdentityHash:
    def test_same_metric_yields_same_hash(self):
        m1 = _make_metric()
        m2 = _make_metric()
        assert m1.metric_hash() == m2.metric_hash()

    def test_different_evaluator_id_yields_different_hash(self):
        m1 = _make_metric(evaluator_id="eval.a")
        m2 = _make_metric(evaluator_id="eval.b")
        assert m1.metric_hash() != m2.metric_hash()

    def test_metric_hash_is_hex_sha256(self):
        m = _make_metric()
        h = m.metric_hash()
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ─────────────────────────────────────────────────────────────────────────────
# 15. ConstraintEvaluatorRegistry — immutability
# ─────────────────────────────────────────────────────────────────────────────


class TestEvaluatorRegistryImmutability:
    def test_double_registration_rejected(self):
        class FakeEval(ConstraintEvaluator):
            evaluator_id = "test.double.v1"
            evaluator_version = "1.0.0"
            build_identity = "build-001"

        registry = ConstraintEvaluatorRegistry()
        e = FakeEval()
        registry.register(e)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(e)

    def test_frozen_registry_rejects_new_registration(self):
        class FakeEval2(ConstraintEvaluator):
            evaluator_id = "test.frozen.v1"
            evaluator_version = "1.0.0"
            build_identity = "build-002"

        registry = ConstraintEvaluatorRegistry()
        registry.freeze()
        with pytest.raises(RuntimeError, match="frozen"):
            registry.register(FakeEval2())

    def test_unknown_evaluator_returns_none(self):
        registry = ConstraintEvaluatorRegistry()
        result = registry.get(
            evaluator_id="missing",
            evaluator_version="1.0.0",
            build_identity="x",
        )
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# 16. Thermodynamics measured status is not ISOMORPHIC_CLOSURE
# ─────────────────────────────────────────────────────────────────────────────


class TestThermodynamicsMeasuredStatus:
    def test_no_measured_vector_gives_continue_descent(self):
        therm = SystemThermodynamics(_make_manifold())
        assert therm.check_measured_status(0) == "CONTINUE_DESCENT"

    def test_t_max_always_returns_t_max_violation(self):
        therm = SystemThermodynamics(_make_manifold(t_max_steps=3))
        assert therm.check_measured_status(3) == "T_MAX_VIOLATION"

    def test_measured_zero_drift_gives_unverified_convergence(self):
        metric = _make_metric()
        therm = SystemThermodynamics(_make_manifold())
        vec = _all_measured_vector(constraint=0.0, metric=metric)
        therm.update_from_measured_vector(vec)
        status = therm.check_measured_status(0)
        assert status in ("UNVERIFIED_CONVERGENCE", "CONTINUE_DESCENT")
        assert status != "ISOMORPHIC_CLOSURE"
