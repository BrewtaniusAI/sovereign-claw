"""
measured_drift.py — Canonical Measured Drift and Verified Closure Types
=======================================================================
Implements the Measured Drift and Verified Closure Contract (issue #17,
docs/MEASURED_DRIFT_CLOSURE.md).

Core invariants enforced here:
 1. Drift is observed, not awarded — a successful call does not reduce drift.
 2. Current drift ≠ historical penalty integral.
 3. UNMEASURED ≠ 0.0 — unknown required values never default to zero.
 4. Executor is not validator — handler return is not independent evidence.
 5. Closure is a predicate over authoritative evidence.
 6. No caller-provided scalar grants authority.
 7. T_MAX expiry is not convergence.
 8. PREDICTED and MEASURED are distinct observation phases.
 9. Evidence is bounded and privacy-safe.
10. Claims match proof — fixed-time guarantee requires stability certificate.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Literal

# ── Measurement state ────────────────────────────────────────────────────────
MeasurementState = Literal["MEASURED", "UNMEASURED"]

# ── Observation phase ────────────────────────────────────────────────────────
ObservationPhase = Literal["BEFORE", "AFTER", "PREDICTED"]

# ── Closure status literals ──────────────────────────────────────────────────
ClosureStatus = Literal[
    "ISOMORPHIC_CLOSURE",  # All conditions met; evidence persisted
    "UNVERIFIED_NO_CLOSURE",  # Evaluator/evidence unavailable
    "UNVERIFIED_CONVERGENCE",  # Measurement exists but no fixed-time certificate
    "BOUNDED_STEP_NO_CLOSURE",  # Bounded controller completed; no verified closure
    "STALLED",  # No validated progress / oscillation detected
    "T_MAX_VIOLATION",  # Step/wall budget expired
    "POLICY_DENIED",  # Policy gate denied
    "EXECUTION_FAILURE",  # Unresolved worker/resource/isolation failure
    "EVIDENCE_FAILURE",  # Required evidence could not be persisted
]

# ── Drift component names ────────────────────────────────────────────────────
REQUIRED_COMPONENTS: frozenset[str] = frozenset(
    {
        "constraint",  # lawful-state distance
        "postcondition",  # postcondition error
        "execution_error",  # execution/worker error
        "policy",  # policy/compliance component
        "provider_uncertainty",  # provider health/uncertainty
        "resource_latency",  # resource/deadline/latency
    }
)

_MAX_EVALUATOR_ID_LEN = 128
_MAX_VERSION_LEN = 64
_MAX_RULE_ID_LEN = 128
_MAX_RULES = 64
_MAX_REFS = 64
_MAX_TRACE_LEN = 128
_MAX_HASH_LEN = 128


def _bounded_str(value: str, max_len: int, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a str, got {type(value).__name__}")
    if len(value) > max_len:
        raise ValueError(f"{label} exceeds maximum length {max_len}: {len(value)}")
    return value


def _finite_float(value: float, label: str) -> float:
    import math

    if not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be numeric, got {type(value).__name__}")
    v = float(value)
    if not math.isfinite(v):
        raise ValueError(f"{label} must be finite, got {v!r}")
    return v


def _bounded_float_01(value: float, label: str) -> float:
    v = _finite_float(value, label)
    if not (0.0 <= v <= 1.0):
        raise ValueError(f"{label} must be in [0.0, 1.0], got {v!r}")
    return v


# ── ComponentMeasurement ─────────────────────────────────────────────────────
@dataclass(frozen=True)
class ComponentMeasurement:
    """
    A single normalized drift component measurement.

    When measurement_state is UNMEASURED, the value field is absent (None).
    UNMEASURED components cannot satisfy closure requirements — they are
    never coerced to zero.
    """

    component: str
    measurement_state: MeasurementState
    value: float | None  # None when UNMEASURED; normalized to [0,1] when MEASURED
    evidence_ref: str | None = None  # bounded reference digest or rule ID

    def __post_init__(self) -> None:
        _bounded_str(self.component, _MAX_EVALUATOR_ID_LEN, "component")
        if self.measurement_state == "MEASURED":
            if self.value is None:
                raise ValueError(f"MEASURED component '{self.component}' must have a value")
            object.__setattr__(
                self, "value", _bounded_float_01(self.value, f"component '{self.component}' value")
            )
        else:  # UNMEASURED
            if self.value is not None:
                raise ValueError(
                    f"UNMEASURED component '{self.component}' must not have a value; "
                    f"got {self.value!r} — UNMEASURED != 0.0"
                )

    @property
    def is_measured(self) -> bool:
        return self.measurement_state == "MEASURED"


# ── DriftMetricIdentity ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class DriftMetricIdentity:
    """
    Versioned binding of drift metric semantics.

    Ties the component set, normalization rules, weights, and closure
    requirements to exact evaluator/implementation identities.
    Without a matching identity, composite scalars are non-authoritative.
    """

    metric_id: str
    metric_version: str
    evaluator_id: str
    evaluator_version: str
    build_identity: str
    required_components: frozenset[str] = field(
        default_factory=lambda: frozenset(REQUIRED_COMPONENTS)
    )
    weights: dict[str, float] = field(default_factory=dict)
    tolerance_identity: str | None = None

    def __post_init__(self) -> None:
        for attr in (
            "metric_id",
            "metric_version",
            "evaluator_id",
            "evaluator_version",
            "build_identity",
        ):
            _bounded_str(getattr(self, attr), _MAX_EVALUATOR_ID_LEN, attr)
        if self.tolerance_identity is not None:
            _bounded_str(self.tolerance_identity, _MAX_EVALUATOR_ID_LEN, "tolerance_identity")
        # weights must be finite
        for k, v in self.weights.items():
            _finite_float(v, f"weight[{k!r}]")

    def metric_hash(self) -> str:
        payload = {
            "metric_id": self.metric_id,
            "metric_version": self.metric_version,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "build_identity": self.build_identity,
            "required_components": sorted(self.required_components),
            "weights": {k: self.weights[k] for k in sorted(self.weights)},
            "tolerance_identity": self.tolerance_identity,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()


# Default metric identity used when no domain-specific metric is configured.
# Composite scalar from this identity is NON-AUTHORITATIVE for closure.
DEFAULT_METRIC_IDENTITY = DriftMetricIdentity(
    metric_id="sovereign.drift.v1",
    metric_version="1.0.0",
    evaluator_id="sovereign.evaluator.none",
    evaluator_version="0.0.0",
    build_identity="unregistered",
    required_components=frozenset(REQUIRED_COMPONENTS),
    weights={},
    tolerance_identity=None,
)


# ── DriftVectorV1 ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DriftVectorV1:
    """
    Instantaneous measured drift vector (production closure authority).

    Distinct from the historical drift integral/report (DriftReport in drift.py),
    which is diagnostic only.

    A component with measurement_state=UNMEASURED cannot contribute to closure
    and must not be coerced to zero.  Missing required components yield
    UNVERIFIED_NO_CLOSURE, never synthetic progress.
    """

    schema_version: str
    trace_id: str
    step_index: int
    observation_phase: ObservationPhase
    metric_identity: DriftMetricIdentity
    components: tuple[ComponentMeasurement, ...]
    timestamp_utc: float

    def __post_init__(self) -> None:
        _bounded_str(self.schema_version, _MAX_VERSION_LEN, "schema_version")
        _bounded_str(self.trace_id, _MAX_TRACE_LEN, "trace_id")
        if self.step_index < 0:
            raise ValueError(f"step_index must be >= 0, got {self.step_index}")
        _finite_float(self.timestamp_utc, "timestamp_utc")

    @property
    def component_map(self) -> dict[str, ComponentMeasurement]:
        return {c.component: c for c in self.components}

    def get_component(self, name: str) -> ComponentMeasurement | None:
        return self.component_map.get(name)

    def all_required_measured(self, required: frozenset[str] | None = None) -> bool:
        required = required or self.metric_identity.required_components
        cm = self.component_map
        return all(name in cm and cm[name].measurement_state == "MEASURED" for name in required)

    def composite_scalar(self) -> float | None:
        """
        Deterministic weighted composite scalar for routing/policy compatibility.

        Returns None (not 0.0) if any required component is UNMEASURED.
        This is a projection of evidence, not the source of evidence for closure.
        """
        required = self.metric_identity.required_components
        if not self.all_required_measured(required):
            return None  # Cannot be 0.0 when evidence is missing

        weights = self.metric_identity.weights
        measured = [
            c for c in self.components if c.measurement_state == "MEASURED" and c.value is not None
        ]
        if not measured:
            return None

        if weights:
            total_weight = sum(weights.get(c.component, 1.0) for c in measured)
            if total_weight <= 0.0:
                return None
            weighted_total = 0.0
            for c in measured:
                if c.value is not None:
                    weighted_total += (weights.get(c.component, 1.0) / total_weight) * c.value
            return weighted_total
        # Equal weights
        total = 0.0
        for c in measured:
            if c.value is not None:
                total += c.value
        return total / len(measured)

    def vector_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "step_index": self.step_index,
            "observation_phase": self.observation_phase,
            "metric_identity_hash": self.metric_identity.metric_hash(),
            "components": [
                {
                    "component": c.component,
                    "measurement_state": c.measurement_state,
                    "value": c.value,
                    "evidence_ref": c.evidence_ref,
                }
                for c in self.components
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()

    @classmethod
    def unmeasured(
        cls,
        *,
        trace_id: str,
        step_index: int,
        metric_identity: DriftMetricIdentity,
        observation_phase: str = "AFTER",
    ) -> DriftVectorV1:
        """
        Construct a fully UNMEASURED drift vector.

        Used when no evaluator is available or evidence is missing.
        The composite scalar of this vector is None (never 0.0).
        """
        components = tuple(
            ComponentMeasurement(
                component=name,
                measurement_state="UNMEASURED",
                value=None,
            )
            for name in sorted(metric_identity.required_components)
        )
        return cls(
            schema_version="sovereign.drift.vector.v1",
            trace_id=trace_id,
            step_index=step_index,
            observation_phase=observation_phase,
            metric_identity=metric_identity,
            components=components,
            timestamp_utc=time.time(),
        )


# ── StateObservationV1 ───────────────────────────────────────────────────────
@dataclass(frozen=True)
class StateObservationV1:
    """
    Server-derived authoritative observation of task state at a single phase.

    Created server-side from authoritative runtime facts only.
    Model/client data cannot substitute or override this observation.
    All fields are bounded, finite, and deterministic.
    Raw result bodies are never stored — only digests, sizes, and bounded metadata.
    """

    schema_version: str
    trace_id: str
    correlation_id: str
    step_index: int
    phase: ObservationPhase

    # Tool execution identity
    tool_id: str
    tool_contract_hash: str
    action_digest: str

    # Execution result (bounded — no raw output bodies)
    worker_status: str
    result_digest: str
    result_size_bytes: int

    # Policy decision (from #20)
    policy_decision: str  # ALLOW or DENY
    policy_context_hash: str
    policy_bundle_hash: str

    # Postcondition result
    postcondition_result: str  # PASS, FAIL, UNKNOWN
    postcondition_validator_id: str
    postcondition_validator_version: str

    # Resource/deadline
    elapsed_ms: float
    remaining_deadline_ms: float
    resource_limit_result: str
    isolation_enforcement_id: str

    # Provider
    provider_identity: str
    provider_uncertainty: float | None  # None if unmeasured; [0,1] if measured

    # Observation hash (computed after construction)
    observation_hash: str = field(default="")

    def __post_init__(self) -> None:
        for attr in (
            "schema_version",
            "trace_id",
            "correlation_id",
            "tool_id",
            "tool_contract_hash",
            "action_digest",
            "worker_status",
            "result_digest",
            "policy_decision",
            "policy_context_hash",
            "policy_bundle_hash",
            "postcondition_result",
            "postcondition_validator_id",
            "postcondition_validator_version",
            "resource_limit_result",
            "isolation_enforcement_id",
            "provider_identity",
        ):
            _bounded_str(getattr(self, attr), _MAX_HASH_LEN, attr)
        if self.step_index < 0:
            raise ValueError("step_index must be >= 0")
        if self.result_size_bytes < 0:
            raise ValueError("result_size_bytes must be >= 0")
        _finite_float(self.elapsed_ms, "elapsed_ms")
        _finite_float(self.remaining_deadline_ms, "remaining_deadline_ms")
        if self.provider_uncertainty is not None:
            _bounded_float_01(self.provider_uncertainty, "provider_uncertainty")
        # Compute hash if not already set
        if not self.observation_hash:
            object.__setattr__(self, "observation_hash", self._compute_hash())

    def _compute_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id,
            "step_index": self.step_index,
            "phase": self.phase,
            "tool_id": self.tool_id,
            "tool_contract_hash": self.tool_contract_hash,
            "action_digest": self.action_digest,
            "worker_status": self.worker_status,
            "result_digest": self.result_digest,
            "result_size_bytes": self.result_size_bytes,
            "policy_decision": self.policy_decision,
            "policy_context_hash": self.policy_context_hash,
            "policy_bundle_hash": self.policy_bundle_hash,
            "postcondition_result": self.postcondition_result,
            "postcondition_validator_id": self.postcondition_validator_id,
            "postcondition_validator_version": self.postcondition_validator_version,
            "elapsed_ms": self.elapsed_ms,
            "remaining_deadline_ms": self.remaining_deadline_ms,
            "resource_limit_result": self.resource_limit_result,
            "isolation_enforcement_id": self.isolation_enforcement_id,
            "provider_identity": self.provider_identity,
            "provider_uncertainty": self.provider_uncertainty,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()

    @property
    def policy_allowed(self) -> bool:
        return self.policy_decision == "ALLOW"

    @property
    def execution_succeeded(self) -> bool:
        return self.worker_status in {"success", "WORKER_SUCCESS"}

    @property
    def postcondition_passed(self) -> bool:
        return self.postcondition_result == "PASS"


# ── ConstraintAssessmentV1 ───────────────────────────────────────────────────
@dataclass(frozen=True)
class ConstraintAssessmentV1:
    """
    Output of a registered ConstraintEvaluator.

    Produced by the server-owned evaluator from authoritative before/after
    observations. Model/client data cannot substitute this.
    """

    schema_version: str
    evaluator_id: str
    evaluator_version: str
    evaluator_build_hash: str
    domain_version: str
    metric_identity: DriftMetricIdentity

    # Component measurements from the evaluator
    component_measurements: tuple[ComponentMeasurement, ...]

    # Postcondition
    postcondition_result: str  # PASS, FAIL, UNKNOWN
    postcondition_rule_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    # Assessment hash
    assessment_hash: str = field(default="")

    def __post_init__(self) -> None:
        for attr in (
            "schema_version",
            "evaluator_id",
            "evaluator_version",
            "evaluator_build_hash",
            "domain_version",
        ):
            _bounded_str(getattr(self, attr), _MAX_EVALUATOR_ID_LEN, attr)
        if len(self.postcondition_rule_ids) > _MAX_RULES:
            raise ValueError(f"postcondition_rule_ids exceeds limit {_MAX_RULES}")
        if len(self.evidence_refs) > _MAX_REFS:
            raise ValueError(f"evidence_refs exceeds limit {_MAX_REFS}")
        for rid in self.postcondition_rule_ids:
            _bounded_str(rid, _MAX_RULE_ID_LEN, "rule_id")
        for ref in self.evidence_refs:
            _bounded_str(ref, _MAX_HASH_LEN, "evidence_ref")
        if not self.assessment_hash:
            object.__setattr__(self, "assessment_hash", self._compute_hash())

    def _compute_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "evaluator_build_hash": self.evaluator_build_hash,
            "domain_version": self.domain_version,
            "metric_identity_hash": self.metric_identity.metric_hash(),
            "components": [
                {
                    "component": c.component,
                    "measurement_state": c.measurement_state,
                    "value": c.value,
                    "evidence_ref": c.evidence_ref,
                }
                for c in self.component_measurements
            ],
            "postcondition_result": self.postcondition_result,
            "postcondition_rule_ids": sorted(self.postcondition_rule_ids),
            "evidence_refs": sorted(self.evidence_refs),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()

    @property
    def postcondition_passed(self) -> bool:
        return self.postcondition_result == "PASS"

    def to_drift_vector(
        self,
        *,
        trace_id: str,
        step_index: int,
    ) -> DriftVectorV1:
        """Convert this assessment into an authoritative MEASURED drift vector."""
        return DriftVectorV1(
            schema_version="sovereign.drift.vector.v1",
            trace_id=trace_id,
            step_index=step_index,
            observation_phase="AFTER",
            metric_identity=self.metric_identity,
            components=self.component_measurements,
            timestamp_utc=time.time(),
        )


# ── ConstraintEvaluator protocol ─────────────────────────────────────────────
class ConstraintEvaluator:
    """
    Server-owned constraint evaluator.

    Evaluators are selected by the server-owned registry keyed by
    (evaluator_id, evaluator_version, build_identity).  Model/client data
    cannot select arbitrary imports or callables.

    Subclass and override evaluate() to implement domain-specific logic.
    """

    evaluator_id: str = "abstract"
    evaluator_version: str = "0.0.0"
    build_identity: str = "abstract"

    def evaluate(
        self,
        *,
        before: StateObservationV1,
        after: StateObservationV1,
        metric_identity: DriftMetricIdentity,
    ) -> ConstraintAssessmentV1:
        raise NotImplementedError


# ── ConstraintEvaluatorRegistry ─────────────────────────────────────────────
class ConstraintEvaluatorRegistry:
    """
    Trusted server-owned registry of constraint evaluators.

    Evaluators are registered at server startup with an immutable binding
    (evaluator_id, evaluator_version, build_identity) -> ConstraintEvaluator.
    No client/model path can replace a registered evaluator.
    """

    def __init__(self) -> None:
        self._registry: dict[tuple[str, str, str], ConstraintEvaluator] = {}
        self._frozen: bool = False

    def register(self, evaluator: ConstraintEvaluator) -> None:
        """Register an evaluator. Raises if the registry is frozen or the ID is already bound."""
        if self._frozen:
            raise RuntimeError("ConstraintEvaluatorRegistry is frozen; no further registrations")
        key = (evaluator.evaluator_id, evaluator.evaluator_version, evaluator.build_identity)
        if key in self._registry:
            raise ValueError(f"Evaluator {key!r} already registered; substitution rejected")
        self._registry[key] = evaluator

    def freeze(self) -> None:
        """Freeze the registry; no further registrations allowed after startup."""
        self._frozen = True

    def get(
        self,
        *,
        evaluator_id: str,
        evaluator_version: str,
        build_identity: str,
    ) -> ConstraintEvaluator | None:
        return self._registry.get((evaluator_id, evaluator_version, build_identity))

    def lookup_for_metric(self, metric_identity: DriftMetricIdentity) -> ConstraintEvaluator | None:
        return self.get(
            evaluator_id=metric_identity.evaluator_id,
            evaluator_version=metric_identity.evaluator_version,
            build_identity=metric_identity.build_identity,
        )

    def evaluate_or_unverified(
        self,
        *,
        before: StateObservationV1,
        after: StateObservationV1,
        metric_identity: DriftMetricIdentity,
        trace_id: str,
        step_index: int,
    ) -> tuple[ConstraintAssessmentV1 | None, DriftVectorV1]:
        """
        Attempt evaluation; return UNMEASURED drift vector if no evaluator exists.

        Returns (assessment, drift_vector). If no evaluator is registered,
        assessment is None and drift_vector has all components UNMEASURED.
        The caller must treat None assessment as UNVERIFIED_NO_CLOSURE.
        """
        evaluator = self.lookup_for_metric(metric_identity)
        if evaluator is None:
            return None, DriftVectorV1.unmeasured(
                trace_id=trace_id,
                step_index=step_index,
                metric_identity=metric_identity,
            )
        try:
            assessment = evaluator.evaluate(
                before=before,
                after=after,
                metric_identity=metric_identity,
            )
        except Exception:  # noqa: BLE001 — evaluator failure is non-fatal; return UNMEASURED
            return None, DriftVectorV1.unmeasured(
                trace_id=trace_id,
                step_index=step_index,
                metric_identity=metric_identity,
            )
        return assessment, assessment.to_drift_vector(trace_id=trace_id, step_index=step_index)


# Module-level default registry (server-populated at startup)
_DEFAULT_REGISTRY = ConstraintEvaluatorRegistry()


def get_default_registry() -> ConstraintEvaluatorRegistry:
    """Return the module-level default evaluator registry."""
    return _DEFAULT_REGISTRY


# ── StabilityCertificateV1 ───────────────────────────────────────────────────
@dataclass(frozen=True)
class StabilityCertificateV1:
    """
    Fixed-time stability certificate bound to exact metric/evaluator/domain identities.

    Required for ISOMORPHIC_CLOSURE when a fixed-time convergence guarantee is
    claimed.  Without a valid certificate, the system must report
    UNVERIFIED_CONVERGENCE, not a fixed-time guarantee.

    Covers the discrete recurrence and runtime assumptions of the codebase;
    the continuous-time Lyapunov expression alone is not sufficient.
    """

    schema_version: str
    certificate_id: str
    metric_identity: DriftMetricIdentity
    evaluator_id: str
    domain_id: str
    domain_version: str

    # ELFE parameters covered by this certificate
    elfe_a: float
    elfe_b: float
    elfe_p: float
    elfe_q: float
    descent_scale: float
    perturbation_bound: float
    tolerance: float

    # Discrete sampling/update interval this certificate was calibrated for
    discrete_update_interval_s: float

    # Proven bounds
    max_steps: int
    max_wall_time_s: float

    # Admissible initial state assumptions
    admissible_initial_drift_max: float

    # Certificate/calibration artifact
    certificate_digest: str
    issued_at_utc: float

    def __post_init__(self) -> None:
        for attr in (
            "schema_version",
            "certificate_id",
            "evaluator_id",
            "domain_id",
            "domain_version",
        ):
            _bounded_str(getattr(self, attr), _MAX_EVALUATOR_ID_LEN, attr)
        _bounded_str(self.certificate_digest, _MAX_HASH_LEN, "certificate_digest")
        for attr in (
            "elfe_a",
            "elfe_b",
            "elfe_p",
            "elfe_q",
            "descent_scale",
            "perturbation_bound",
            "tolerance",
            "discrete_update_interval_s",
            "max_wall_time_s",
            "admissible_initial_drift_max",
            "issued_at_utc",
        ):
            _finite_float(getattr(self, attr), attr)
        if self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")

    def matches_metric(self, metric_identity: DriftMetricIdentity) -> bool:
        return metric_identity.metric_hash() == self.metric_identity.metric_hash()

    def is_stale(self, current_utc: float | None = None, max_age_s: float = 86400 * 90) -> bool:
        """Returns True if the certificate is older than max_age_s (default 90 days)."""
        t = current_utc if current_utc is not None else time.time()
        return (t - self.issued_at_utc) > max_age_s


# ── ClosureDecisionV1 ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ClosureDecisionV1:
    """
    Immutable server-owned closure decision.

    ISOMORPHIC_CLOSURE is granted only when ALL applicable conditions are met
    and evidence is persisted.  All other outcomes are distinct non-closure
    statuses.

    A terminal T_MAX_VIOLATION, policy denial, evidence failure, or execution
    failure can never later be relabeled as closure by a numeric snap.
    """

    schema_version: str
    trace_id: str
    step_index: int
    status: ClosureStatus

    # Evidence chain
    drift_vector_hash: str
    assessment_hash: str | None
    before_observation_hash: str
    after_observation_hash: str
    policy_context_hash: str
    policy_bundle_hash: str
    vault_evidence_ref: str | None

    # Metric/evaluator identity bound to this decision
    metric_identity: DriftMetricIdentity
    evaluator_id: str | None
    stability_certificate_id: str | None

    # Failure reasons
    failure_reasons: tuple[str, ...]

    # Decision hash
    decision_hash: str = field(default="")

    def __post_init__(self) -> None:
        _bounded_str(self.schema_version, _MAX_VERSION_LEN, "schema_version")
        _bounded_str(self.trace_id, _MAX_TRACE_LEN, "trace_id")
        if self.step_index < 0:
            raise ValueError("step_index must be >= 0")
        for attr in (
            "drift_vector_hash",
            "before_observation_hash",
            "after_observation_hash",
            "policy_context_hash",
            "policy_bundle_hash",
        ):
            _bounded_str(getattr(self, attr), _MAX_HASH_LEN, attr)
        if not self.decision_hash:
            object.__setattr__(self, "decision_hash", self._compute_hash())

    def _compute_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "step_index": self.step_index,
            "status": self.status,
            "drift_vector_hash": self.drift_vector_hash,
            "assessment_hash": self.assessment_hash,
            "before_observation_hash": self.before_observation_hash,
            "after_observation_hash": self.after_observation_hash,
            "policy_context_hash": self.policy_context_hash,
            "policy_bundle_hash": self.policy_bundle_hash,
            "vault_evidence_ref": self.vault_evidence_ref,
            "metric_identity_hash": self.metric_identity.metric_hash(),
            "evaluator_id": self.evaluator_id,
            "stability_certificate_id": self.stability_certificate_id,
            "failure_reasons": sorted(self.failure_reasons),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()

    @property
    def is_closure(self) -> bool:
        return self.status == "ISOMORPHIC_CLOSURE"

    @property
    def is_terminal_violation(self) -> bool:
        return self.status in (
            "T_MAX_VIOLATION",
            "POLICY_DENIED",
            "EXECUTION_FAILURE",
            "EVIDENCE_FAILURE",
        )


# ── LaneTransitionEvidenceV1 ─────────────────────────────────────────────────
@dataclass(frozen=True)
class LaneTransitionEvidenceV1:
    """
    Server-derived immutable evidence for lane transitions.

    Replaces caller-authoritative advance(approved, drift) semantics.
    No drift == 0.0, NaN/negative/fabricated scalar, or caller approved=True
    may jump REFLEX -> AUTHORITATIVE or create closure.
    AUTHORITATIVE output is a lane/state, not proof of closure.
    """

    schema_version: str
    trace_id: str
    prior_lane: str
    target_lane: str
    transition_rule: str

    # Measured evidence (not caller-supplied)
    drift_vector_hash: str
    closure_status: ClosureStatus
    policy_decision: str
    policy_context_hash: str
    policy_bundle_hash: str
    vault_evidence_ref: str | None
    step_index: int
    deadline_remaining_ms: float

    # Evidence hash
    evidence_hash: str = field(default="")

    def __post_init__(self) -> None:
        for attr in (
            "schema_version",
            "trace_id",
            "prior_lane",
            "target_lane",
            "transition_rule",
            "drift_vector_hash",
            "policy_decision",
            "policy_context_hash",
            "policy_bundle_hash",
        ):
            _bounded_str(getattr(self, attr), _MAX_HASH_LEN, attr)
        if self.step_index < 0:
            raise ValueError("step_index must be >= 0")
        _finite_float(self.deadline_remaining_ms, "deadline_remaining_ms")
        if not self.evidence_hash:
            object.__setattr__(self, "evidence_hash", self._compute_hash())

    def _compute_hash(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "prior_lane": self.prior_lane,
            "target_lane": self.target_lane,
            "transition_rule": self.transition_rule,
            "drift_vector_hash": self.drift_vector_hash,
            "closure_status": self.closure_status,
            "policy_decision": self.policy_decision,
            "policy_context_hash": self.policy_context_hash,
            "policy_bundle_hash": self.policy_bundle_hash,
            "vault_evidence_ref": self.vault_evidence_ref,
            "step_index": self.step_index,
            "deadline_remaining_ms": self.deadline_remaining_ms,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()


# ── Closure predicate ────────────────────────────────────────────────────────
def evaluate_closure(
    *,
    drift_vector: DriftVectorV1,
    assessment: ConstraintAssessmentV1 | None,
    before_observation: StateObservationV1,
    after_observation: StateObservationV1,
    policy_context_hash: str,
    policy_bundle_hash: str,
    vault_evidence_ref: str | None,
    stability_certificate: StabilityCertificateV1 | None = None,
    constraint_threshold: float = 0.0,
) -> ClosureDecisionV1:
    """
    Server-owned closure predicate.

    Returns a ClosureDecisionV1 with the appropriate status.
    ISOMORPHIC_CLOSURE requires ALL of:
      - Observation phase is AFTER (MEASURED, not PREDICTED)
      - Required components are all MEASURED
      - Measured constraint distance within evaluator threshold
      - Independent postcondition passed
      - Evaluator/assessment present and valid
      - Policy is ALLOW and evidence persisted
      - No unresolved execution/resource failure
      - Vault evidence persisted

    All other outcomes are distinct non-closure statuses.
    A terminal violation cannot be relabeled as closure.
    """
    trace_id = drift_vector.trace_id
    step_index = drift_vector.step_index
    metric_identity = drift_vector.metric_identity
    failure_reasons: list[str] = []
    _has_execution_failure: bool = False

    # Check observation phase — must be AFTER (MEASURED execution, not PREDICTED)
    if drift_vector.observation_phase != "AFTER":
        failure_reasons.append(
            f"observation_phase={drift_vector.observation_phase!r}; must be AFTER for closure"
        )

    # Check execution succeeded (no unresolved worker failure)
    if not after_observation.execution_succeeded:
        _has_execution_failure = True
        failure_reasons.append(
            f"worker_status={after_observation.worker_status!r}; unresolved execution failure"
        )

    # Check policy allowed
    if not after_observation.policy_allowed:
        failure_reasons.append(
            f"policy_decision={after_observation.policy_decision!r}; policy gate denied"
        )
        return ClosureDecisionV1(
            schema_version="sovereign.closure.v1",
            trace_id=trace_id,
            step_index=step_index,
            status="POLICY_DENIED",
            drift_vector_hash=drift_vector.vector_hash(),
            assessment_hash=assessment.assessment_hash if assessment else None,
            before_observation_hash=before_observation.observation_hash,
            after_observation_hash=after_observation.observation_hash,
            policy_context_hash=policy_context_hash,
            policy_bundle_hash=policy_bundle_hash,
            vault_evidence_ref=vault_evidence_ref,
            metric_identity=metric_identity,
            evaluator_id=assessment.evaluator_id if assessment else None,
            stability_certificate_id=None,
            failure_reasons=tuple(failure_reasons),
        )

    # Check evaluator/assessment present
    if assessment is None:
        failure_reasons.append("no registered evaluator for metric identity; cannot assess closure")
        return ClosureDecisionV1(
            schema_version="sovereign.closure.v1",
            trace_id=trace_id,
            step_index=step_index,
            status="UNVERIFIED_NO_CLOSURE",
            drift_vector_hash=drift_vector.vector_hash(),
            assessment_hash=None,
            before_observation_hash=before_observation.observation_hash,
            after_observation_hash=after_observation.observation_hash,
            policy_context_hash=policy_context_hash,
            policy_bundle_hash=policy_bundle_hash,
            vault_evidence_ref=vault_evidence_ref,
            metric_identity=metric_identity,
            evaluator_id=None,
            stability_certificate_id=None,
            failure_reasons=tuple(failure_reasons),
        )

    # Check all required components are MEASURED (not UNMEASURED)
    if not drift_vector.all_required_measured():
        unmeasured = [
            name
            for name in metric_identity.required_components
            if name not in drift_vector.component_map
            or drift_vector.component_map[name].measurement_state == "UNMEASURED"
        ]
        failure_reasons.append(f"UNMEASURED required components: {unmeasured!r}")
        return ClosureDecisionV1(
            schema_version="sovereign.closure.v1",
            trace_id=trace_id,
            step_index=step_index,
            status="UNVERIFIED_NO_CLOSURE",
            drift_vector_hash=drift_vector.vector_hash(),
            assessment_hash=assessment.assessment_hash,
            before_observation_hash=before_observation.observation_hash,
            after_observation_hash=after_observation.observation_hash,
            policy_context_hash=policy_context_hash,
            policy_bundle_hash=policy_bundle_hash,
            vault_evidence_ref=vault_evidence_ref,
            metric_identity=metric_identity,
            evaluator_id=assessment.evaluator_id,
            stability_certificate_id=None,
            failure_reasons=tuple(failure_reasons),
        )

    # Check postcondition passed (independent of executor self-certification)
    if not assessment.postcondition_passed:
        failure_reasons.append(f"postcondition_result={assessment.postcondition_result!r}")

    # Check vault evidence persisted before final closure
    if vault_evidence_ref is None:
        failure_reasons.append(
            "vault_evidence_ref is None; evidence persistence required for closure"
        )

    # Check unresolved execution failure
    if _has_execution_failure:
        return ClosureDecisionV1(
            schema_version="sovereign.closure.v1",
            trace_id=trace_id,
            step_index=step_index,
            status="EXECUTION_FAILURE",
            drift_vector_hash=drift_vector.vector_hash(),
            assessment_hash=assessment.assessment_hash,
            before_observation_hash=before_observation.observation_hash,
            after_observation_hash=after_observation.observation_hash,
            policy_context_hash=policy_context_hash,
            policy_bundle_hash=policy_bundle_hash,
            vault_evidence_ref=vault_evidence_ref,
            metric_identity=metric_identity,
            evaluator_id=assessment.evaluator_id,
            stability_certificate_id=None,
            failure_reasons=tuple(failure_reasons),
        )

    if vault_evidence_ref is None:
        return ClosureDecisionV1(
            schema_version="sovereign.closure.v1",
            trace_id=trace_id,
            step_index=step_index,
            status="EVIDENCE_FAILURE",
            drift_vector_hash=drift_vector.vector_hash(),
            assessment_hash=assessment.assessment_hash,
            before_observation_hash=before_observation.observation_hash,
            after_observation_hash=after_observation.observation_hash,
            policy_context_hash=policy_context_hash,
            policy_bundle_hash=policy_bundle_hash,
            vault_evidence_ref=None,
            metric_identity=metric_identity,
            evaluator_id=assessment.evaluator_id,
            stability_certificate_id=None,
            failure_reasons=tuple(failure_reasons),
        )

    # Check constraint distance within threshold
    constraint_component = drift_vector.get_component("constraint")
    if constraint_component is None or constraint_component.measurement_state == "UNMEASURED":
        failure_reasons.append("constraint component UNMEASURED; cannot verify closure threshold")
    elif (
        constraint_component.value is not None and constraint_component.value > constraint_threshold
    ):
        failure_reasons.append(
            f"constraint distance {constraint_component.value!r} > threshold {constraint_threshold!r}"
        )

    if failure_reasons:
        # Check if a stability certificate is required but missing
        if stability_certificate is None:
            return ClosureDecisionV1(
                schema_version="sovereign.closure.v1",
                trace_id=trace_id,
                step_index=step_index,
                status="UNVERIFIED_CONVERGENCE",
                drift_vector_hash=drift_vector.vector_hash(),
                assessment_hash=assessment.assessment_hash,
                before_observation_hash=before_observation.observation_hash,
                after_observation_hash=after_observation.observation_hash,
                policy_context_hash=policy_context_hash,
                policy_bundle_hash=policy_bundle_hash,
                vault_evidence_ref=vault_evidence_ref,
                metric_identity=metric_identity,
                evaluator_id=assessment.evaluator_id,
                stability_certificate_id=None,
                failure_reasons=tuple(failure_reasons),
            )
        return ClosureDecisionV1(
            schema_version="sovereign.closure.v1",
            trace_id=trace_id,
            step_index=step_index,
            status="BOUNDED_STEP_NO_CLOSURE",
            drift_vector_hash=drift_vector.vector_hash(),
            assessment_hash=assessment.assessment_hash,
            before_observation_hash=before_observation.observation_hash,
            after_observation_hash=after_observation.observation_hash,
            policy_context_hash=policy_context_hash,
            policy_bundle_hash=policy_bundle_hash,
            vault_evidence_ref=vault_evidence_ref,
            metric_identity=metric_identity,
            evaluator_id=assessment.evaluator_id,
            stability_certificate_id=stability_certificate.certificate_id,
            failure_reasons=tuple(failure_reasons),
        )

    # Check stability certificate if fixed-time is claimed
    cert_id = None
    if stability_certificate is not None:
        if stability_certificate.is_stale():
            failure_reasons.append("stability_certificate is stale; UNVERIFIED_CONVERGENCE")
        elif not stability_certificate.matches_metric(metric_identity):
            failure_reasons.append("stability_certificate metric identity mismatch")
        else:
            cert_id = stability_certificate.certificate_id

        if failure_reasons:
            return ClosureDecisionV1(
                schema_version="sovereign.closure.v1",
                trace_id=trace_id,
                step_index=step_index,
                status="UNVERIFIED_CONVERGENCE",
                drift_vector_hash=drift_vector.vector_hash(),
                assessment_hash=assessment.assessment_hash,
                before_observation_hash=before_observation.observation_hash,
                after_observation_hash=after_observation.observation_hash,
                policy_context_hash=policy_context_hash,
                policy_bundle_hash=policy_bundle_hash,
                vault_evidence_ref=vault_evidence_ref,
                metric_identity=metric_identity,
                evaluator_id=assessment.evaluator_id,
                stability_certificate_id=None,
                failure_reasons=tuple(failure_reasons),
            )

    # All conditions met — emit ISOMORPHIC_CLOSURE
    return ClosureDecisionV1(
        schema_version="sovereign.closure.v1",
        trace_id=trace_id,
        step_index=step_index,
        status="ISOMORPHIC_CLOSURE",
        drift_vector_hash=drift_vector.vector_hash(),
        assessment_hash=assessment.assessment_hash,
        before_observation_hash=before_observation.observation_hash,
        after_observation_hash=after_observation.observation_hash,
        policy_context_hash=policy_context_hash,
        policy_bundle_hash=policy_bundle_hash,
        vault_evidence_ref=vault_evidence_ref,
        metric_identity=metric_identity,
        evaluator_id=assessment.evaluator_id,
        stability_certificate_id=cert_id,
        failure_reasons=(),
    )
