import dataclasses
import time

import pytest

from sovereign_claw.lanes import Lane, LaneRouter
from sovereign_claw.measured_closure import (
    ComponentMeasurement,
    ConstraintAssessment,
    EvaluatorRegistry,
    MeasurementState,
    REQUIRED_COMPONENTS,
    StabilityCertificate,
    TrustedCertificateRegistry,
    authority_hash,
    evaluate_closure,
)
from sovereign_claw.proof_vault import LedgerIntegrityError, ProofVault
from sovereign_claw.thermodynamics import SystemThermodynamics, TaskManifold


def _component(identity: str, trace_id: str, evidence_hash: str) -> ComponentMeasurement:
    return ComponentMeasurement(
        identity, 0.0, MeasurementState.MEASURED, evidence_hash, trace_id, time.time()
    )


def _decision(vault: ProofVault, trace_id: str):
    records = {}
    for identity in sorted(REQUIRED_COMPONENTS):
        record = vault.append_authority_event(
            "component.measurement.v1", trace_id, {"identity": identity, "value": 0.0}
        )
        records[identity] = record.record_hash
    assessment = ConstraintAssessment(
        trace_id,
        "step-1",
        "metric-v1",
        "evaluator-v1",
        tuple(_component(key, trace_id, value) for key, value in records.items()),
    )
    verified = vault.verify_component_measurements(assessment)
    decision = evaluate_closure(assessment, verified_components=verified, vault=vault)
    payload = {
        "closure_decision_hash": decision.decision_hash,
        "closure_status": decision.status,
        "assessment_hash": decision.assessment_hash,
        "drift_metric_identity": decision.metric_identity,
        "evaluator_identity": decision.evaluator_identity,
        "step_id": decision.step_id,
    }
    evidence = vault.append_authority_event("closure.decision.v1", trace_id, payload)
    binding = vault.verify_evidence_binding(
        evidence.record_hash,
        trace_id=trace_id,
        evidence_type="authority.closure.decision.v1",
        **payload,
    )
    return decision, binding


def test_arbitrary_or_absent_evidence_cannot_close(tmp_path):
    vault = ProofVault(tmp_path / "vault.db")
    trace = vault.create_trace("objective")
    decision, _ = _decision(vault, trace)
    with pytest.raises(LedgerIntegrityError):
        vault.verify_evidence_binding(
            "a" * 64,
            trace_id=trace,
            evidence_type="authority.closure.decision.v1",
            closure_decision_hash=decision.decision_hash,
            closure_status=decision.status,
            assessment_hash=decision.assessment_hash,
            drift_metric_identity=decision.metric_identity,
            evaluator_identity=decision.evaluator_identity,
            step_id=decision.step_id,
        )


def test_wrong_trace_and_type_cannot_close(tmp_path):
    vault = ProofVault(tmp_path / "vault.db")
    trace = vault.create_trace("objective")
    decision, binding = _decision(vault, trace)
    for changes in ({"trace_id": "wrong"}, {"evidence_type": "authority.wrong"}):
        kwargs = {
            "trace_id": trace,
            "evidence_type": binding.evidence_type,
            "closure_decision_hash": decision.decision_hash,
            "closure_status": decision.status,
            "assessment_hash": decision.assessment_hash,
            "drift_metric_identity": decision.metric_identity,
            "evaluator_identity": decision.evaluator_identity,
            "step_id": decision.step_id,
            **changes,
        }
        with pytest.raises(LedgerIntegrityError):
            vault.verify_evidence_binding(binding.record_hash, **kwargs)


def test_forged_lane_binding_and_scalar_zero_are_non_authoritative(tmp_path):
    vault = ProofVault(tmp_path / "vault.db")
    trace = vault.create_trace("objective")
    decision, binding = _decision(vault, trace)
    forged = dataclasses.replace(binding, record_hash="f" * 64)
    router = LaneRouter()
    assert router.advance(approved=True, drift=0.0) is Lane.DELIBERATE
    with pytest.raises(LedgerIntegrityError):
        router.authorize_closure(vault, decision, forged)
    assert router.authorize_closure(vault, decision, binding) is Lane.AUTHORITATIVE


def test_provider_none_remains_unmeasured_and_blocks_closure():
    trace = "trace"
    components = [
        _component(identity, trace, identity * 4)
        for identity in REQUIRED_COMPONENTS
        if identity != "provider_uncertainty"
    ]
    components.append(
        ComponentMeasurement(
            "provider_uncertainty", None, MeasurementState.UNMEASURED, None, trace, time.time()
        )
    )
    assessment = ConstraintAssessment(trace, "step", "metric", "evaluator", tuple(components))
    # A deliberately incomplete verifier-shaped value is sufficient to exercise
    # UNKNOWN preservation; only ProofVault can mint production instances.
    from sovereign_claw.measured_closure import VerifiedComponentEvidenceV1

    verified = VerifiedComponentEvidenceV1(
        trace,
        assessment.assessment_hash,
        frozenset(item.evidence_record_hash for item in components if item.evidence_record_hash),
        "diagnostic",
        object(),
    )
    decision = evaluate_closure(assessment, verified_components=verified, vault=object())
    assert decision.status == "NOT_CLOSED"
    assert "UNMEASURED:provider_uncertainty" in decision.reasons


def test_measured_component_requires_evidence_and_predicted_cannot_replay():
    with pytest.raises(ValueError):
        ComponentMeasurement("constraint", 0.0, MeasurementState.MEASURED, None, "t", time.time())
    with pytest.raises(ValueError):
        ComponentMeasurement("constraint", 0.0, MeasurementState.PREDICTED, None, "t", time.time())


def test_scalar_thermodynamics_zero_is_legacy_and_tmax_latches():
    therm = SystemThermodynamics(TaskManifold("objective", t_max_steps=1))
    therm.current_drift = 0.0
    assert therm.check_isomorphic_state(0) == "LEGACY_MODEL_ZERO"
    assert therm.check_isomorphic_state(1) == "T_MAX_VIOLATION"
    assert therm.check_isomorphic_state(0) == "T_MAX_VIOLATION"


def test_certificate_trust_is_server_owned_exact_and_bound():
    now = time.time()
    base = {
        "certificate_id": "cert",
        "metric_identity": "metric",
        "evaluator_identity": "eval",
        "runtime_build_identity": "build",
        "coefficients": {"a": 1.0},
        "assumptions": ["bounded"],
        "domain": "domain",
        "version": "1",
        "valid_from": now - 1,
        "valid_until": now + 1,
    }
    digest = authority_hash(base)
    certificate = StabilityCertificate(**base, artifact_digest=digest)
    with pytest.raises(PermissionError):
        TrustedCertificateRegistry([digest])
    registry = TrustedCertificateRegistry([digest], server_owned=True)
    assert registry.verify(
        certificate,
        metric_identity="metric",
        evaluator_identity="eval",
        runtime_build_identity="build",
        now=now,
    )
    assert not registry.verify(
        certificate,
        metric_identity="wrong",
        evaluator_identity="eval",
        runtime_build_identity="build",
        now=now,
    )
    assert not registry.verify(
        certificate,
        metric_identity="metric",
        evaluator_identity="eval",
        runtime_build_identity="build",
        now=now + 2,
    )


def test_evaluator_mutation_after_freeze_rejected():
    def evaluator():
        return 1

    registry = EvaluatorRegistry(server_owned=True)
    registry.register("eval", evaluator)
    registry.freeze()
    assert registry.resolve("eval")() == 1
    evaluator.__code__ = (lambda: 2).__code__
    with pytest.raises(RuntimeError, match="changed"):
        registry.resolve("eval")


def test_binding_revalidates_deterministically_after_restart(tmp_path):
    path = tmp_path / "vault.db"
    first = ProofVault(path)
    trace = first.create_trace("objective")
    decision, _ = _decision(first, trace)
    second = ProofVault(path)
    record = second.get_evidence_records(trace)[-1]
    binding = second.verify_evidence_binding(
        record.record_hash,
        trace_id=trace,
        evidence_type="authority.closure.decision.v1",
        closure_decision_hash=decision.decision_hash,
        closure_status=decision.status,
        assessment_hash=decision.assessment_hash,
        drift_metric_identity=decision.metric_identity,
        evaluator_identity=decision.evaluator_identity,
        step_id=decision.step_id,
    )
    assert second.revalidate_evidence_binding(binding, decision)
