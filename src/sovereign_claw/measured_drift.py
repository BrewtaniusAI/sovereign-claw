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
import math
import time
from dataclasses import dataclass, field
from typing import Literal

# ── Measurement state ────────────────────────────────────────────────────────
MeasurementState = Literal["MEASURED", "UNMEASURED"]
_VALID_MEASUREMENT_STATES: frozenset[str] = frozenset({"MEASURED", "UNMEASURED"})

# ── Observation phase ────────────────────────────────────────────────────────
ObservationPhase = Literal["BEFORE", "AFTER", "PREDICTED"]
_VALID_OBSERVATION_PHASES: frozenset[str] = frozenset({"BEFORE", "AFTER", "PREDICTED"})

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
_VALID_CLOSURE_STATUSES: frozenset[str] = frozenset(
    {
        "ISOMORPHIC_CLOSURE",
        "UNVERIFIED_NO_CLOSURE",
        "UNVERIFIED_CONVERGENCE",
        "BOUNDED_STEP_NO_CLOSURE",
        "STALLED",
        "T_MAX_VIOLATION",
        "POLICY_DENIED",
        "EXECUTION_FAILURE",
        "EVIDENCE_FAILURE",
    }
)
_TERMINAL_VIOLATION_STATUSES: frozenset[str] = frozenset(
    {"T_MAX_VIOLATION", "POLICY_DENIED", "EXECUTION_FAILURE", "EVIDENCE_FAILURE", "STALLED"}
)

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

# ── Component-level closure safety bounds (authoritative per metric) ─────────
# When a component's measured value exceeds this bound, closure is denied
# even if the constraint component is near zero.  These are the default
# server-side safety bounds; domain evaluators may tighten them via
# DriftMetricIdentity.component_closure_bounds.
_DEFAULT_COMPONENT_CLOSURE_BOUNDS: dict[str, float] = {
    "constraint": 0.0,  # must be exactly ≤ threshold (per metric)
    "postcondition": 0.0,  # postcondition error must be zero for closure
    "execution_error": 0.0,  # no unresolved execution error at closure
    "policy": 0.0,  # no policy violation at closure
    "provider_uncertainty": 0.25,  # >25% uncertainty blocks closure by default
    "resource_latency": 0.5,  # >50% normalized latency blocks closure by default
}

_MAX_EVALUATOR_ID_LEN = 128
_MAX_VERSION_LEN = 64
_MAX_RULE_ID_LEN = 128
_MAX_RULES = 64
_MAX_REFS = 64
_MAX_TRACE_LEN = 128
_MAX_HASH_LEN = 128
_MAX_COMPONENT_BOUNDS = 64
# Maximum number of failure_reasons entries in ClosureDecisionV1
_MAX_FAILURE_REASONS = 32
# Maximum individual failure_reason string length
_MAX_FAILURE_REASON_LEN = 512

# Validated policy_decision values for StateObservationV1
_VALID_POLICY_DECISIONS: frozenset[str] = frozenset({"ALLOW", "DENY"})
# Validated postcondition_result values for StateObservationV1 / ConstraintAssessmentV1
_VALID_POSTCONDITION_RESULTS: frozenset[str] = frozenset({"PASS", "FAIL", "UNKNOWN"})
# Validated worker_status values for StateObservationV1
# BEFORE observations must use "pending"; AFTER observations use "success" or "failure".
_VALID_WORKER_STATUSES: frozenset[str] = frozenset({"pending", "success", "failure"})
# Validated resource_limit_result values for StateObservationV1
# "pending" is the BEFORE phase sentinel; others are AFTER enforcement results.
_VALID_RESOURCE_RESULTS: frozenset[str] = frozenset(
    {"pending", "ok", "failure", "UNAVAILABLE", "UNSUPPORTED", "UNVERIFIED"}
)


def _bounded_str(value: str, max_len: int, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a str, got {type(value).__name__}")
    if len(value) > max_len:
        raise ValueError(f"{label} exceeds maximum length {max_len}: {len(value)}")
    return value


def _reject_bool(value: object, label: str) -> None:
    """Reject bool values for numeric fields; bool is a subtype of int in Python."""
    if isinstance(value, bool):
        raise TypeError(f"{label} must be a numeric type, not bool")


def _finite_float(value: float, label: str) -> float:
    _reject_bool(value, label)
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


def _exact_int(value: object, label: str) -> int:
    """Accept only exact int (not bool, not float) for integer fields."""
    if isinstance(value, bool):
        raise TypeError(f"{label} must be int, not bool")
    if not isinstance(value, int):
        raise TypeError(f"{label} must be int, got {type(value).__name__}")
    return int(value)


def _compute_vector_provenance_hash(
    *,
    assessment_hash: str,
    before_observation_hash: str,
    after_observation_hash: str,
    action_digest: str,
    tool_id: str,
    tool_contract_hash: str,
    policy_context_hash: str,
    policy_bundle_hash: str,
) -> str:
    """
    Compute a cryptographic provenance hash that binds a DriftVectorV1 to the exact
    assessment/observations/action/policy that produced it.

    This prevents a valid assessment being paired with a separately fabricated safer
    vector under the same metric/trace to manufacture closure.
    """
    payload = {
        "assessment_hash": assessment_hash,
        "before_observation_hash": before_observation_hash,
        "after_observation_hash": after_observation_hash,
        "action_digest": action_digest,
        "tool_id": tool_id,
        "tool_contract_hash": tool_contract_hash,
        "policy_context_hash": policy_context_hash,
        "policy_bundle_hash": policy_bundle_hash,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()


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
        if self.evidence_ref is not None:
            _bounded_str(self.evidence_ref, _MAX_HASH_LEN, "evidence_ref")
        if self.measurement_state not in _VALID_MEASUREMENT_STATES:
            raise ValueError(
                f"measurement_state must be one of {sorted(_VALID_MEASUREMENT_STATES)!r}, "
                f"got {self.measurement_state!r}"
            )
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

    ``weights`` and ``component_closure_bounds`` are stored as immutable
    frozensets of (key, value) tuples so that ``DriftMetricIdentity`` is
    truly deep-frozen.  Use ``weights_map`` / ``closure_bounds_map``
    for dict-access.
    """

    metric_id: str
    metric_version: str
    evaluator_id: str
    evaluator_version: str
    build_identity: str
    required_components: frozenset[str] = field(
        default_factory=lambda: frozenset(REQUIRED_COMPONENTS)
    )
    # Immutable weights: frozenset of (component, weight) pairs
    weights: frozenset[tuple[str, float]] = field(default_factory=frozenset)
    tolerance_identity: str | None = None
    # Per-component safety bounds for closure: frozenset of (component, bound) pairs.
    # A MEASURED component exceeding its bound blocks ISOMORPHIC_CLOSURE.
    # Defaults to server-wide _DEFAULT_COMPONENT_CLOSURE_BOUNDS if empty.
    component_closure_bounds: frozenset[tuple[str, float]] = field(default_factory=frozenset)

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
        if len(self.required_components) > _MAX_COMPONENT_BOUNDS:
            raise ValueError(f"required_components exceeds limit {_MAX_COMPONENT_BOUNDS}")
        # Validate weights entries
        if len(self.weights) > _MAX_COMPONENT_BOUNDS:
            raise ValueError(f"weights exceeds limit {_MAX_COMPONENT_BOUNDS}")
        weight_keys = [k for k, _ in self.weights]
        if len(weight_keys) != len(set(weight_keys)):
            raise ValueError("duplicate weight keys")
        for k, v in self.weights:
            if not isinstance(k, str):
                raise TypeError(f"weights key must be str, got {type(k).__name__}")
            _finite_float(v, f"weight[{k!r}]")
        # Validate component_closure_bounds entries
        if len(self.component_closure_bounds) > _MAX_COMPONENT_BOUNDS:
            raise ValueError(f"component_closure_bounds exceeds limit {_MAX_COMPONENT_BOUNDS}")
        bound_keys = [k for k, _ in self.component_closure_bounds]
        if len(bound_keys) != len(set(bound_keys)):
            raise ValueError("duplicate component_closure_bounds keys")
        for k, v in self.component_closure_bounds:
            if not isinstance(k, str):
                raise TypeError(f"component_closure_bounds key must be str, got {type(k).__name__}")
            _bounded_float_01(v, f"component_closure_bounds[{k!r}]")
        # Defect #9: Every required component must have an explicit closure bound,
        # either in component_closure_bounds or in _DEFAULT_COMPONENT_CLOSURE_BOUNDS.
        # Custom required components without any bound are rejected.
        _bound_key_set: set[str] = {k for k, _ in self.component_closure_bounds}
        for comp in self.required_components:
            if comp not in _bound_key_set and comp not in _DEFAULT_COMPONENT_CLOSURE_BOUNDS:
                raise ValueError(
                    f"required_component {comp!r} has no closure bound; "
                    "add an explicit entry in component_closure_bounds "
                    "or use a standard component name"
                )
        # Defect #9: Weights must be non-negative; if any weight is present, total must be > 0.
        # Negative-weight metrics are rejected as they could manufacture closure from unsafe values.
        if self.weights:
            for k, v in self.weights:
                if v < 0.0:
                    raise ValueError(
                        f"weight[{k!r}]={v!r} is negative; negative weights are not permitted "
                        "for authoritative composite projection"
                    )
            total_w = sum(v for _, v in self.weights)
            if total_w <= 0.0:
                raise ValueError(
                    f"weights sum to {total_w!r}; total must be positive for composite projection"
                )

    @property
    def weights_map(self) -> dict[str, float]:
        """Return weights as a dict (derived; not stored as mutable dict)."""
        return dict(self.weights)

    @property
    def closure_bounds_map(self) -> dict[str, float]:
        """Return per-component closure bounds as a dict.

        Falls back to _DEFAULT_COMPONENT_CLOSURE_BOUNDS for missing entries.
        """
        result = dict(_DEFAULT_COMPONENT_CLOSURE_BOUNDS)
        result.update(self.component_closure_bounds)
        return result

    def metric_hash(self) -> str:
        payload = {
            "metric_id": self.metric_id,
            "metric_version": self.metric_version,
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "build_identity": self.build_identity,
            "required_components": sorted(self.required_components),
            "weights": sorted((k, v) for k, v in self.weights),
            "tolerance_identity": self.tolerance_identity,
            "component_closure_bounds": sorted((k, v) for k, v in self.component_closure_bounds),
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
    weights=frozenset(),
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
    # Defect #3: Cryptographic provenance binding this vector to the exact
    # assessment/observations/action/policy that produced it.  None for UNMEASURED
    # vectors; required and verified by evaluate_closure() when assessment is present.
    provenance_hash: str | None = None

    def __post_init__(self) -> None:
        _bounded_str(self.schema_version, _MAX_VERSION_LEN, "schema_version")
        _bounded_str(self.trace_id, _MAX_TRACE_LEN, "trace_id")
        _exact_int(self.step_index, "step_index")
        if self.step_index < 0:
            raise ValueError(f"step_index must be >= 0, got {self.step_index}")
        if len(self.components) > _MAX_COMPONENT_BOUNDS:
            raise ValueError(f"components exceeds limit {_MAX_COMPONENT_BOUNDS}")
        if self.observation_phase not in _VALID_OBSERVATION_PHASES:
            raise ValueError(
                f"observation_phase must be one of {sorted(_VALID_OBSERVATION_PHASES)!r}, "
                f"got {self.observation_phase!r}"
            )
        _finite_float(self.timestamp_utc, "timestamp_utc")
        # Reject duplicate component names
        names = [c.component for c in self.components]
        if len(names) != len(set(names)):
            dupes = [n for n in set(names) if names.count(n) > 1]
            raise ValueError(f"duplicate component names in DriftVectorV1: {dupes!r}")
        if self.provenance_hash is not None:
            _bounded_str(self.provenance_hash, _MAX_HASH_LEN, "provenance_hash")

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

        weights = self.metric_identity.weights_map
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
            "provenance_hash": self.provenance_hash,
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
        observation_phase: ObservationPhase = "AFTER",
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

    # Defect #8: Bounded server-derived side-effect evidence digest for the AFTER phase.
    # For BEFORE observations this is None.  For AFTER observations on governed subprocess
    # tools, this is the SHA-256 hex of the canonical side_effect_evidence dict.
    # Required to be present (non-None, non-empty) in AFTER observations on governed paths
    # where the ToolSpec declares required_side_effects; otherwise may be None.
    side_effect_digest: str | None = None

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
        if self.phase not in _VALID_OBSERVATION_PHASES:
            raise ValueError(
                f"phase must be one of {sorted(_VALID_OBSERVATION_PHASES)!r}, got {self.phase!r}"
            )
        _exact_int(self.step_index, "step_index")
        if self.step_index < 0:
            raise ValueError("step_index must be >= 0")
        _exact_int(self.result_size_bytes, "result_size_bytes")
        if self.result_size_bytes < 0:
            raise ValueError("result_size_bytes must be >= 0")
        _finite_float(self.elapsed_ms, "elapsed_ms")
        _finite_float(self.remaining_deadline_ms, "remaining_deadline_ms")
        if self.provider_uncertainty is not None:
            _bounded_float_01(self.provider_uncertainty, "provider_uncertainty")
        # Defect #10: Runtime-validate measured authority enums
        if self.policy_decision not in _VALID_POLICY_DECISIONS:
            raise ValueError(
                f"policy_decision must be one of {sorted(_VALID_POLICY_DECISIONS)!r}, "
                f"got {self.policy_decision!r}"
            )
        if self.postcondition_result not in _VALID_POSTCONDITION_RESULTS:
            raise ValueError(
                f"postcondition_result must be one of {sorted(_VALID_POSTCONDITION_RESULTS)!r}, "
                f"got {self.postcondition_result!r}"
            )
        # Defect #9: worker_status and resource_limit_result must be from bounded domains.
        if self.worker_status not in _VALID_WORKER_STATUSES:
            raise ValueError(
                f"worker_status must be one of {sorted(_VALID_WORKER_STATUSES)!r}, "
                f"got {self.worker_status!r}"
            )
        if self.resource_limit_result not in _VALID_RESOURCE_RESULTS:
            raise ValueError(
                f"resource_limit_result must be one of {sorted(_VALID_RESOURCE_RESULTS)!r}, "
                f"got {self.resource_limit_result!r}"
            )
        if self.side_effect_digest is not None:
            _bounded_str(self.side_effect_digest, _MAX_HASH_LEN, "side_effect_digest")
        # Compute the authoritative hash
        computed = self._compute_hash()
        if self.observation_hash:
            # If caller supplied a hash, verify it matches the canonical computation
            if self.observation_hash != computed:
                raise ValueError(
                    "observation_hash does not match canonical recomputation; "
                    "supply an empty string to have the hash computed automatically"
                )
        else:
            object.__setattr__(self, "observation_hash", computed)

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
            "side_effect_digest": self.side_effect_digest,
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
    before_observation_hash: str
    after_observation_hash: str
    trace_id: str
    action_digest: str
    tool_id: str
    tool_contract_hash: str
    policy_context_hash: str
    policy_bundle_hash: str

    # Assessment hash
    assessment_hash: str = field(default="")

    def __post_init__(self) -> None:
        for attr in (
            "schema_version",
            "evaluator_id",
            "evaluator_version",
            "evaluator_build_hash",
            "domain_version",
            "before_observation_hash",
            "after_observation_hash",
            "trace_id",
            "action_digest",
            "tool_id",
            "tool_contract_hash",
            "policy_context_hash",
            "policy_bundle_hash",
        ):
            _bounded_str(getattr(self, attr), _MAX_EVALUATOR_ID_LEN, attr)
        if len(self.component_measurements) > _MAX_COMPONENT_BOUNDS:
            raise ValueError(f"component_measurements exceeds limit {_MAX_COMPONENT_BOUNDS}")
        if len(self.postcondition_rule_ids) > _MAX_RULES:
            raise ValueError(f"postcondition_rule_ids exceeds limit {_MAX_RULES}")
        if len(self.evidence_refs) > _MAX_REFS:
            raise ValueError(f"evidence_refs exceeds limit {_MAX_REFS}")
        for rid in self.postcondition_rule_ids:
            _bounded_str(rid, _MAX_RULE_ID_LEN, "rule_id")
        for ref in self.evidence_refs:
            _bounded_str(ref, _MAX_HASH_LEN, "evidence_ref")
        # Reject duplicate component names
        comp_names = [c.component for c in self.component_measurements]
        if len(comp_names) != len(set(comp_names)):
            dupes = [n for n in set(comp_names) if comp_names.count(n) > 1]
            raise ValueError(f"duplicate component names in ConstraintAssessmentV1: {dupes!r}")
        # Defect #9: postcondition_result must be from bounded domain
        if self.postcondition_result not in _VALID_POSTCONDITION_RESULTS:
            raise ValueError(
                f"ConstraintAssessmentV1.postcondition_result must be one of "
                f"{sorted(_VALID_POSTCONDITION_RESULTS)!r}, got {self.postcondition_result!r}"
            )
        computed = self._compute_hash()
        if self.assessment_hash:
            if self.assessment_hash != computed:
                raise ValueError(
                    "assessment_hash does not match canonical recomputation; "
                    "supply an empty string to have the hash computed automatically"
                )
        else:
            object.__setattr__(self, "assessment_hash", computed)

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
            "before_observation_hash": self.before_observation_hash,
            "after_observation_hash": self.after_observation_hash,
            "trace_id": self.trace_id,
            "action_digest": self.action_digest,
            "tool_id": self.tool_id,
            "tool_contract_hash": self.tool_contract_hash,
            "policy_context_hash": self.policy_context_hash,
            "policy_bundle_hash": self.policy_bundle_hash,
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
        before_observation_hash: str = "",
        after_observation_hash: str = "",
        action_digest: str = "",
        tool_id: str = "",
        tool_contract_hash: str = "",
        policy_context_hash: str = "",
        policy_bundle_hash: str = "",
    ) -> DriftVectorV1:
        """Convert this assessment into an authoritative MEASURED drift vector.

        Defect #3: When the observation/action/policy provenance is supplied,
        compute and bind the provenance_hash so that evaluate_closure() can verify
        the vector was produced from the exact assessment and observations.
        """
        prov_hash: str | None = None
        if before_observation_hash and after_observation_hash:
            prov_hash = _compute_vector_provenance_hash(
                assessment_hash=self.assessment_hash,
                before_observation_hash=before_observation_hash,
                after_observation_hash=after_observation_hash,
                action_digest=action_digest or self.action_digest,
                tool_id=tool_id or self.tool_id,
                tool_contract_hash=tool_contract_hash or self.tool_contract_hash,
                policy_context_hash=policy_context_hash or self.policy_context_hash,
                policy_bundle_hash=policy_bundle_hash or self.policy_bundle_hash,
            )
        return DriftVectorV1(
            schema_version="sovereign.drift.vector.v1",
            trace_id=trace_id,
            step_index=step_index,
            observation_phase="AFTER",
            metric_identity=self.metric_identity,
            components=self.component_measurements,
            timestamp_utc=time.time(),
            provenance_hash=prov_hash,
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

    def bind_assessment(
        self,
        assessment: ConstraintAssessmentV1,
        *,
        before: StateObservationV1,
        after: StateObservationV1,
        metric_identity: DriftMetricIdentity,
    ) -> ConstraintAssessmentV1:
        """
        Verify that the returned assessment's bindings match the supplied
        observations/action/policy/metric exactly.

        Defect #2: Reject mismatched assessments outright rather than rebinding/
        laundering them.  A returned assessment whose bindings are not already exact
        must be rejected (raises ValueError → evaluate_or_unverified returns UNMEASURED).
        Never manufacture a new trusted binding around stale measurements.
        """
        if (
            assessment.before_observation_hash == before.observation_hash
            and assessment.after_observation_hash == after.observation_hash
            and assessment.trace_id == before.trace_id
            and assessment.action_digest == after.action_digest
            and assessment.tool_id == after.tool_id
            and assessment.tool_contract_hash == after.tool_contract_hash
            and assessment.policy_context_hash == after.policy_context_hash
            and assessment.policy_bundle_hash == after.policy_bundle_hash
            and assessment.metric_identity.metric_hash() == metric_identity.metric_hash()
        ):
            return assessment
        raise ValueError(
            "ConstraintEvaluator returned an assessment whose observation/action/tool/"
            "contract/policy/metric bindings do not match the supplied before/after "
            "observations exactly; rebinding rejected — mismatched assessment yields "
            "UNVERIFIED_NO_CLOSURE"
        )


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
            assessment = evaluator.bind_assessment(
                assessment,
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
        return assessment, assessment.to_drift_vector(
            trace_id=trace_id,
            step_index=step_index,
            before_observation_hash=before.observation_hash,
            after_observation_hash=after.observation_hash,
            action_digest=after.action_digest,
            tool_id=after.tool_id,
            tool_contract_hash=after.tool_contract_hash,
            policy_context_hash=after.policy_context_hash,
            policy_bundle_hash=after.policy_bundle_hash,
        )


# Module-level default registry (server-populated at startup)
_DEFAULT_REGISTRY = ConstraintEvaluatorRegistry()


def get_default_registry() -> ConstraintEvaluatorRegistry:
    """Return the module-level default evaluator registry."""
    return _DEFAULT_REGISTRY


# ── StabilityCertificateV1 ───────────────────────────────────────────────────
@dataclass(frozen=True)
class StabilityCertificateV1:
    """
    Fixed-time stability certificate bound to exact metric/evaluator/domain/runtime identities.

    Required for ISOMORPHIC_CLOSURE when a fixed-time convergence guarantee is
    claimed.  Without a valid certificate, the system must report
    UNVERIFIED_CONVERGENCE, not a fixed-time guarantee.

    Covers the discrete recurrence and runtime assumptions of the codebase;
    the continuous-time Lyapunov expression alone is not sufficient.

    Fixed-time wording/status must only be emitted when the runtime configuration
    matches this certificate's exact identities; otherwise report
    UNVERIFIED_CONVERGENCE / bounded-step semantics.
    """

    schema_version: str
    certificate_id: str
    metric_identity: DriftMetricIdentity

    # Exact evaluator/build identities this certificate was issued for
    evaluator_id: str
    evaluator_version: str
    evaluator_build_identity: str

    domain_id: str
    domain_version: str

    # Controller/recurrence implementation identity
    controller_implementation_id: str
    controller_implementation_version: str

    # ELFE parameters covered by this certificate
    elfe_a: float  # must be > 0
    elfe_b: float  # must be > 0
    elfe_p: float  # must be 0 < p < 1
    elfe_q: float  # must be q > 1
    descent_scale: float  # must be > 0
    perturbation_bound: float  # must be >= 0
    tolerance: float  # must be >= 0

    # Discrete sampling/update interval this certificate was calibrated for
    discrete_update_interval_s: float  # must be > 0

    # Oscillation detection policy identity (e.g., hash of the oscillation detection config)
    oscillation_policy_id: str

    # Proven bounds
    max_steps: int  # must be >= 1
    max_wall_time_s: float  # must be > 0

    # Admissible initial state assumptions
    admissible_initial_drift_max: float  # must be >= 0

    # Proof/calibration artifact identity
    proof_artifact_id: str
    certificate_digest: str
    issued_at_utc: float

    def __post_init__(self) -> None:
        for attr in (
            "schema_version",
            "certificate_id",
            "evaluator_id",
            "evaluator_version",
            "evaluator_build_identity",
            "domain_id",
            "domain_version",
            "controller_implementation_id",
            "controller_implementation_version",
            "oscillation_policy_id",
            "proof_artifact_id",
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
        # Coefficient range validation
        if self.elfe_a <= 0:
            raise ValueError(f"elfe_a must be > 0, got {self.elfe_a!r}")
        if self.elfe_b <= 0:
            raise ValueError(f"elfe_b must be > 0, got {self.elfe_b!r}")
        if not (0 < self.elfe_p < 1):
            raise ValueError(f"elfe_p must be in (0, 1), got {self.elfe_p!r}")
        if self.elfe_q <= 1:
            raise ValueError(f"elfe_q must be > 1, got {self.elfe_q!r}")
        if self.descent_scale <= 0:
            raise ValueError(f"descent_scale must be > 0, got {self.descent_scale!r}")
        if self.perturbation_bound < 0:
            raise ValueError(f"perturbation_bound must be >= 0, got {self.perturbation_bound!r}")
        if self.tolerance < 0:
            raise ValueError(f"tolerance must be >= 0, got {self.tolerance!r}")
        if self.discrete_update_interval_s <= 0:
            raise ValueError(
                f"discrete_update_interval_s must be > 0, got {self.discrete_update_interval_s!r}"
            )
        if self.admissible_initial_drift_max < 0:
            raise ValueError(
                f"admissible_initial_drift_max must be >= 0, "
                f"got {self.admissible_initial_drift_max!r}"
            )
        _exact_int(self.max_steps, "max_steps")
        if self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if self.max_wall_time_s <= 0:
            raise ValueError(f"max_wall_time_s must be > 0, got {self.max_wall_time_s!r}")
        # Evaluator identity must match the bound metric identity
        if (
            self.evaluator_id != self.metric_identity.evaluator_id
            or self.evaluator_version != self.metric_identity.evaluator_version
            or self.evaluator_build_identity != self.metric_identity.build_identity
        ):
            raise ValueError(
                "StabilityCertificateV1 evaluator identity must match metric_identity evaluator: "
                f"cert={self.evaluator_id}/{self.evaluator_version}/{self.evaluator_build_identity} "
                f"metric={self.metric_identity.evaluator_id}/{self.metric_identity.evaluator_version}"
                f"/{self.metric_identity.build_identity}"
            )
        # Defect #12: Compute and verify certificate_digest from canonical certificate material.
        # The digest must be derived from the certificate's own fields, not trusted as
        # an arbitrary string.  Supply an empty string to have the digest computed automatically.
        computed_digest = self._compute_certificate_digest()
        if self.certificate_digest:
            if self.certificate_digest != computed_digest:
                raise ValueError(
                    "certificate_digest does not match canonical recomputation; "
                    "supply an empty string to have the digest computed automatically"
                )
        else:
            object.__setattr__(self, "certificate_digest", computed_digest)

    def _compute_certificate_digest(self) -> str:
        """Compute a deterministic digest over canonical certificate material."""
        payload = {
            "schema_version": self.schema_version,
            "certificate_id": self.certificate_id,
            "metric_identity_hash": self.metric_identity.metric_hash(),
            "evaluator_id": self.evaluator_id,
            "evaluator_version": self.evaluator_version,
            "evaluator_build_identity": self.evaluator_build_identity,
            "domain_id": self.domain_id,
            "domain_version": self.domain_version,
            "controller_implementation_id": self.controller_implementation_id,
            "controller_implementation_version": self.controller_implementation_version,
            "elfe_a": self.elfe_a,
            "elfe_b": self.elfe_b,
            "elfe_p": self.elfe_p,
            "elfe_q": self.elfe_q,
            "descent_scale": self.descent_scale,
            "perturbation_bound": self.perturbation_bound,
            "tolerance": self.tolerance,
            "discrete_update_interval_s": self.discrete_update_interval_s,
            "oscillation_policy_id": self.oscillation_policy_id,
            "max_steps": self.max_steps,
            "max_wall_time_s": self.max_wall_time_s,
            "admissible_initial_drift_max": self.admissible_initial_drift_max,
            "proof_artifact_id": self.proof_artifact_id,
            "issued_at_utc": self.issued_at_utc,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()

    def matches_metric(self, metric_identity: DriftMetricIdentity) -> bool:
        return metric_identity.metric_hash() == self.metric_identity.metric_hash()

    def matches_configuration(
        self,
        *,
        controller_id: str,
        controller_version: str,
        oscillation_policy_id: str,
        discrete_update_interval_s: float,
        elfe_a: float,
        elfe_b: float,
        elfe_p: float,
        elfe_q: float,
        descent_scale: float,
        perturbation_bound: float,
        tolerance: float,
        max_steps: int,
        max_wall_time_s: float,
        admissible_initial_drift_max: float,
        proof_artifact_id: str,
    ) -> bool:
        """Return True only when the runtime controller/recurrence configuration
        exactly matches the identities this certificate was issued for.

        All fields that define the recurrence assumption must match.  If the
        governed Orchestrator does not match, it must not emit fixed-time
        guarantee semantics; only bounded/unverified semantics are allowed.
        Missing or mismatched proof_artifact_id, ELFE parameters, step/time
        bounds, or admissible initial state all prevent a fixed-time claim.
        """
        return (
            self.controller_implementation_id == controller_id
            and self.controller_implementation_version == controller_version
            and self.oscillation_policy_id == oscillation_policy_id
            and self.discrete_update_interval_s == discrete_update_interval_s
            and self.elfe_a == elfe_a
            and self.elfe_b == elfe_b
            and self.elfe_p == elfe_p
            and self.elfe_q == elfe_q
            and self.descent_scale == descent_scale
            and self.perturbation_bound == perturbation_bound
            and self.tolerance == tolerance
            and self.max_steps == max_steps
            and self.max_wall_time_s == max_wall_time_s
            and self.admissible_initial_drift_max == admissible_initial_drift_max
            and self.proof_artifact_id == proof_artifact_id
        )

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
        if self.status not in _VALID_CLOSURE_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(_VALID_CLOSURE_STATUSES)!r}, got {self.status!r}"
            )
        _exact_int(self.step_index, "step_index")
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
        # Defect #9: Bound optional evidence/ID fields and failure_reasons collection.
        if self.assessment_hash is not None:
            _bounded_str(self.assessment_hash, _MAX_HASH_LEN, "assessment_hash")
        if self.vault_evidence_ref is not None:
            _bounded_str(self.vault_evidence_ref, _MAX_HASH_LEN, "vault_evidence_ref")
        if self.evaluator_id is not None:
            _bounded_str(self.evaluator_id, _MAX_EVALUATOR_ID_LEN, "evaluator_id")
        if self.stability_certificate_id is not None:
            _bounded_str(
                self.stability_certificate_id, _MAX_EVALUATOR_ID_LEN, "stability_certificate_id"
            )
        if len(self.failure_reasons) > _MAX_FAILURE_REASONS:
            raise ValueError(
                f"failure_reasons exceeds maximum count {_MAX_FAILURE_REASONS}; "
                f"got {len(self.failure_reasons)}"
            )
        for i, reason in enumerate(self.failure_reasons):
            if not isinstance(reason, str):
                raise TypeError(f"failure_reasons[{i}] must be str")
            if len(reason) > _MAX_FAILURE_REASON_LEN:
                raise ValueError(
                    f"failure_reasons[{i}] exceeds maximum length {_MAX_FAILURE_REASON_LEN}"
                )
        computed = self._compute_hash()
        if self.decision_hash:
            if self.decision_hash != computed:
                raise ValueError(
                    "decision_hash does not match canonical recomputation; "
                    "supply an empty string to have the hash computed automatically"
                )
        else:
            object.__setattr__(self, "decision_hash", computed)

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
            "STALLED",
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
    closure_decision_hash: str
    action_digest: str
    postcondition_validator_id: str
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
            "closure_decision_hash",
            "action_digest",
            "postcondition_validator_id",
        ):
            _bounded_str(getattr(self, attr), _MAX_HASH_LEN, attr)
        if self.closure_status not in _VALID_CLOSURE_STATUSES:
            raise ValueError(
                f"closure_status must be one of {sorted(_VALID_CLOSURE_STATUSES)!r}, "
                f"got {self.closure_status!r}"
            )
        _exact_int(self.step_index, "step_index")
        if self.step_index < 0:
            raise ValueError("step_index must be >= 0")
        _finite_float(self.deadline_remaining_ms, "deadline_remaining_ms")
        computed = self._compute_hash()
        if self.evidence_hash:
            if self.evidence_hash != computed:
                raise ValueError(
                    "evidence_hash does not match canonical recomputation; "
                    "supply an empty string to have the hash computed automatically"
                )
        else:
            object.__setattr__(self, "evidence_hash", computed)

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
            "closure_decision_hash": self.closure_decision_hash,
            "action_digest": self.action_digest,
            "postcondition_validator_id": self.postcondition_validator_id,
            "vault_evidence_ref": self.vault_evidence_ref,
            "step_index": self.step_index,
            "deadline_remaining_ms": self.deadline_remaining_ms,
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
                "utf-8"
            )
        ).hexdigest()


# ── StabilityRuntimeConfig ───────────────────────────────────────────────────
@dataclass(frozen=True)
class StabilityRuntimeConfig:
    """Runtime controller/recurrence configuration for certificate matching.

    Pass to ``evaluate_closure()`` so it can call
    ``StabilityCertificateV1.matches_configuration()`` against the actual
    runtime parameters.  If absent or if the match fails, fixed-time
    semantics are suppressed and the decision is downgraded to
    ``UNVERIFIED_CONVERGENCE``.
    """

    controller_id: str
    controller_version: str
    oscillation_policy_id: str
    discrete_update_interval_s: float
    elfe_a: float
    elfe_b: float
    elfe_p: float
    elfe_q: float
    descent_scale: float
    perturbation_bound: float
    tolerance: float
    max_steps: int
    max_wall_time_s: float
    admissible_initial_drift_max: float
    proof_artifact_id: str


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
    # Defect #7: Runtime configuration required to validate the certificate against
    # the actual controller/recurrence.  If absent or mismatched, fixed-time semantics
    # are suppressed and the decision is downgraded to UNVERIFIED_CONVERGENCE.
    stability_runtime_config: StabilityRuntimeConfig | None = None,
    # Deprecated free-parameter threshold: ignored when metric_identity defines bounds.
    # Kept for backward-compatibility with tests; metric-bound takes precedence.
    constraint_threshold: float = 0.0,
    # Terminal state inputs — dominant and irreversible
    t_max_violated: bool = False,
    stalled: bool = False,
) -> ClosureDecisionV1:
    """
    Server-owned closure predicate (hardened — issue #17 audit).

    Returns a ClosureDecisionV1 with the appropriate status.
    ISOMORPHIC_CLOSURE requires ALL of:
      - Observation phase is AFTER (MEASURED, not PREDICTED)
      - t_max_violated and stalled are False
      - Required components are all MEASURED
      - Cross-record identity match: before/after share same trace/correlation/
        tool/contract/action; assessment metric matches drift metric; policy
        hashes from the observation match the supplied hashes
      - All applicable per-component safety bounds satisfied (from metric)
      - Measured constraint distance within metric-bound threshold
      - Independent postcondition passed
      - Evaluator/assessment present with matching metric identity
      - Policy is ALLOW and evidence persisted
      - No unresolved execution/resource failure
      - Vault evidence persisted

    All other outcomes are distinct non-closure statuses.
    A terminal T_MAX/STALLED/execution/evidence/policy violation is dominant
    and irreversible — it can never be overwritten by a numeric snap.
    """
    trace_id = drift_vector.trace_id
    step_index = drift_vector.step_index
    metric_identity = drift_vector.metric_identity
    failure_reasons: list[str] = []
    _has_execution_failure: bool = False

    def _make_decision(status: ClosureStatus, *, eval_id: str | None = None) -> ClosureDecisionV1:
        return ClosureDecisionV1(
            schema_version="sovereign.closure.v1",
            trace_id=trace_id,
            step_index=step_index,
            status=status,
            drift_vector_hash=drift_vector.vector_hash(),
            assessment_hash=assessment.assessment_hash if assessment else None,
            before_observation_hash=before_observation.observation_hash,
            after_observation_hash=after_observation.observation_hash,
            policy_context_hash=policy_context_hash,
            policy_bundle_hash=policy_bundle_hash,
            vault_evidence_ref=vault_evidence_ref,
            metric_identity=metric_identity,
            evaluator_id=eval_id or (assessment.evaluator_id if assessment else None),
            stability_certificate_id=None,
            failure_reasons=tuple(failure_reasons),
        )

    # ── Terminal state checks — dominant and irreversible ─────────────────────
    if t_max_violated:
        failure_reasons.append("t_max_violated=True; step/wall budget expired")
        return _make_decision("T_MAX_VIOLATION")
    if stalled:
        failure_reasons.append("stalled=True; oscillation or no validated progress detected")
        return _make_decision("STALLED")

    # ── Observation phase — must be AFTER ────────────────────────────────────
    if drift_vector.observation_phase != "AFTER":
        failure_reasons.append(
            f"observation_phase={drift_vector.observation_phase!r}; must be AFTER for closure"
        )
        return _make_decision("UNVERIFIED_NO_CLOSURE")

    # Defect #4: Enforce observation phases inside the closure predicate.
    # BEFORE must be BEFORE, AFTER must be AFTER. A PREDICTED observation can
    # never be replayed as either side of measured closure evidence.
    if before_observation.phase != "BEFORE":
        failure_reasons.append(
            f"before_observation.phase={before_observation.phase!r}; must be BEFORE — "
            "PREDICTED observations cannot be replayed as BEFORE closure evidence"
        )
        return _make_decision("UNVERIFIED_NO_CLOSURE")
    if after_observation.phase != "AFTER":
        failure_reasons.append(
            f"after_observation.phase={after_observation.phase!r}; must be AFTER — "
            "PREDICTED observations cannot be replayed as AFTER closure evidence"
        )
        return _make_decision("UNVERIFIED_NO_CLOSURE")

    # ── Cross-record identity checks (Fix #6) ────────────────────────────────
    # before/after must share trace_id, correlation_id, tool_id, tool_contract_hash,
    # and action_digest to prove they describe the same governed execution.
    if before_observation.trace_id != after_observation.trace_id:
        failure_reasons.append(
            "before/after trace_id mismatch: "
            f"{before_observation.trace_id!r} != {after_observation.trace_id!r}"
        )
    if before_observation.correlation_id != after_observation.correlation_id:
        failure_reasons.append(
            "before/after correlation_id mismatch: "
            f"{before_observation.correlation_id!r} != {after_observation.correlation_id!r}"
        )
    if before_observation.tool_id != after_observation.tool_id:
        failure_reasons.append(
            "before/after tool_id mismatch: "
            f"{before_observation.tool_id!r} != {after_observation.tool_id!r}"
        )
    if before_observation.tool_contract_hash != after_observation.tool_contract_hash:
        failure_reasons.append(
            "before/after tool_contract_hash mismatch: "
            f"{before_observation.tool_contract_hash!r} != {after_observation.tool_contract_hash!r}"
        )
    if before_observation.action_digest != after_observation.action_digest:
        failure_reasons.append(
            "before/after action_digest mismatch: "
            f"{before_observation.action_digest!r} != {after_observation.action_digest!r}"
        )
    # Verify drift vector trace_id matches observations
    if drift_vector.trace_id != after_observation.trace_id:
        failure_reasons.append(
            "drift vector trace_id does not match observation: "
            f"{drift_vector.trace_id!r} != {after_observation.trace_id!r}"
        )
    # Verify policy hashes match the observation's persisted policy decision
    if after_observation.policy_context_hash != policy_context_hash:
        failure_reasons.append(
            "policy_context_hash mismatch with after_observation: "
            f"{after_observation.policy_context_hash!r} != {policy_context_hash!r}"
        )
    if after_observation.policy_bundle_hash != policy_bundle_hash:
        failure_reasons.append(
            "policy_bundle_hash mismatch with after_observation: "
            f"{after_observation.policy_bundle_hash!r} != {policy_bundle_hash!r}"
        )

    if failure_reasons:
        return _make_decision("UNVERIFIED_NO_CLOSURE")

    # ── Execution status ──────────────────────────────────────────────────────
    if not after_observation.execution_succeeded:
        _has_execution_failure = True
        failure_reasons.append(
            f"worker_status={after_observation.worker_status!r}; unresolved execution failure"
        )

    # ── Policy gate ───────────────────────────────────────────────────────────
    if not after_observation.policy_allowed:
        failure_reasons.append(
            f"policy_decision={after_observation.policy_decision!r}; policy gate denied"
        )
        return _make_decision("POLICY_DENIED")

    # ── Evaluator/assessment present ─────────────────────────────────────────
    if assessment is None:
        failure_reasons.append("no registered evaluator for metric identity; cannot assess closure")
        return _make_decision("UNVERIFIED_NO_CLOSURE")

    # Assessment metric identity must match the drift vector's metric identity
    if assessment.metric_identity.metric_hash() != metric_identity.metric_hash():
        failure_reasons.append(
            "assessment metric_identity hash does not match drift vector metric_identity; "
            "mix-and-match records rejected"
        )
        return _make_decision("UNVERIFIED_NO_CLOSURE", eval_id=assessment.evaluator_id)

    # Assessment evaluator ID/version must match metric identity's evaluator binding
    if (
        assessment.evaluator_id != metric_identity.evaluator_id
        or assessment.evaluator_version != metric_identity.evaluator_version
    ):
        failure_reasons.append(
            "assessment evaluator_id/version does not match metric_identity evaluator binding: "
            f"assessment={assessment.evaluator_id}/{assessment.evaluator_version}; "
            f"metric={metric_identity.evaluator_id}/{metric_identity.evaluator_version}"
        )
        return _make_decision("UNVERIFIED_NO_CLOSURE", eval_id=assessment.evaluator_id)
    if assessment.before_observation_hash != before_observation.observation_hash:
        failure_reasons.append("assessment before_observation_hash mismatch")
        return _make_decision("UNVERIFIED_NO_CLOSURE", eval_id=assessment.evaluator_id)
    if assessment.after_observation_hash != after_observation.observation_hash:
        failure_reasons.append("assessment after_observation_hash mismatch")
        return _make_decision("UNVERIFIED_NO_CLOSURE", eval_id=assessment.evaluator_id)
    if assessment.trace_id != drift_vector.trace_id:
        failure_reasons.append("assessment trace_id mismatch with drift vector")
        return _make_decision("UNVERIFIED_NO_CLOSURE", eval_id=assessment.evaluator_id)
    if assessment.action_digest != after_observation.action_digest:
        failure_reasons.append("assessment action_digest mismatch with after_observation")
        return _make_decision("UNVERIFIED_NO_CLOSURE", eval_id=assessment.evaluator_id)
    if assessment.evaluator_build_hash != metric_identity.build_identity:
        failure_reasons.append("assessment evaluator_build_hash mismatch with metric identity")
        return _make_decision("UNVERIFIED_NO_CLOSURE", eval_id=assessment.evaluator_id)

    # Defect #3: Verify that the drift vector was produced from the exact assessment
    # and observations (provenance binding).  A fabricated safer vector under the same
    # metric/trace cannot substitute for a genuine assessment-derived vector.
    expected_prov = _compute_vector_provenance_hash(
        assessment_hash=assessment.assessment_hash,
        before_observation_hash=before_observation.observation_hash,
        after_observation_hash=after_observation.observation_hash,
        action_digest=after_observation.action_digest,
        tool_id=after_observation.tool_id,
        tool_contract_hash=after_observation.tool_contract_hash,
        policy_context_hash=policy_context_hash,
        policy_bundle_hash=policy_bundle_hash,
    )
    if drift_vector.provenance_hash is None:
        failure_reasons.append(
            "drift_vector.provenance_hash is None; a provenance binding to the exact "
            "assessment/observations/action/policy is required for closure"
        )
        return _make_decision("UNVERIFIED_NO_CLOSURE", eval_id=assessment.evaluator_id)
    if drift_vector.provenance_hash != expected_prov:
        failure_reasons.append(
            "drift_vector.provenance_hash does not match expected value computed from "
            "assessment and observations; fabricated or mis-paired vector rejected"
        )
        return _make_decision("UNVERIFIED_NO_CLOSURE", eval_id=assessment.evaluator_id)

    # Defect #8: Verify AFTER observation side_effect_digest is present when the
    # after observation records a governed tool execution (non-empty tool_contract_hash).
    # Missing side-effect evidence blocks closure.
    if after_observation.tool_contract_hash and after_observation.side_effect_digest is None:
        failure_reasons.append(
            "after_observation.side_effect_digest is None for governed tool execution; "
            "required side-effect evidence is missing — closure blocked"
        )
        return _make_decision("EVIDENCE_FAILURE", eval_id=assessment.evaluator_id)

    # ── All required components must be MEASURED ─────────────────────────────
    if not drift_vector.all_required_measured():
        unmeasured = [
            name
            for name in metric_identity.required_components
            if name not in drift_vector.component_map
            or drift_vector.component_map[name].measurement_state == "UNMEASURED"
        ]
        failure_reasons.append(f"UNMEASURED required components: {unmeasured!r}")
        return _make_decision("UNVERIFIED_NO_CLOSURE", eval_id=assessment.evaluator_id)

    # ── Postcondition ─────────────────────────────────────────────────────────
    if not assessment.postcondition_passed:
        failure_reasons.append(f"postcondition_result={assessment.postcondition_result!r}")

    # ── Vault evidence ────────────────────────────────────────────────────────
    if vault_evidence_ref is None:
        failure_reasons.append(
            "vault_evidence_ref is None; evidence persistence required for closure"
        )

    # ── Unresolved execution failure (dominant after checks above) ────────────
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

    # ── Per-component safety bounds (from metric identity) ────────────────────
    # Use metric-bound threshold for constraint; fall back to legacy parameter only
    # when the metric does not override it (ensures backward compat with tests).
    component_map = drift_vector.component_map
    bounds = metric_identity.closure_bounds_map
    for comp_name, bound in bounds.items():
        comp = component_map.get(comp_name)
        if comp is None or not comp.is_measured:
            continue  # unmeasured components are handled by the all_required_measured check
        if comp.value is not None and comp.value > bound:
            failure_reasons.append(
                f"component {comp_name!r} value {comp.value!r} exceeds "
                f"closure safety bound {bound!r}"
            )

    # If no metric-level constraint bound is set, use the caller-supplied threshold
    constraint_component = component_map.get("constraint")
    if not any(k == "constraint" for k, _ in metric_identity.component_closure_bounds):
        # Fall back to caller-supplied threshold (legacy / test backward compat)
        if constraint_component is None or not constraint_component.is_measured:
            failure_reasons.append(
                "constraint component UNMEASURED; cannot verify closure threshold"
            )
        elif (
            constraint_component.value is not None
            and constraint_component.value > constraint_threshold
        ):
            failure_reasons.append(
                f"constraint distance {constraint_component.value!r} > "
                f"threshold {constraint_threshold!r}"
            )

    if failure_reasons:
        # Defect #6: Only return UNVERIFIED_CONVERGENCE for genuinely measured
        # convergence whose fixed-time/stability claim is unverified — i.e., all
        # hard conditions (postcondition, component bounds, execution) are met but
        # no stability certificate is supplied.  Postcondition failure, unsafe
        # component bounds, or execution failures are hard failures that must
        # remain BOUNDED_STEP_NO_CLOSURE regardless of certificate presence.
        # Reserve UNVERIFIED_CONVERGENCE exclusively for the case where convergence
        # is measured but no fixed-time certificate is attached.
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
            stability_certificate_id=None,
            failure_reasons=tuple(failure_reasons),
        )

    # ── Stability certificate check ───────────────────────────────────────────
    # Defect #7: Call matches_configuration() to require exact runtime match.
    # A stale, metric-mismatched, or config-mismatched certificate cannot support
    # a fixed-time claim.  All mismatches downgrade to UNVERIFIED_CONVERGENCE.
    # When no certificate is provided, ordinary verified closure (ISOMORPHIC_CLOSURE
    # without a fixed-time certificate) is returned — this is the "ordinary verified
    # closure" case where convergence evidence is complete but no fixed-time proof
    # has been attached.
    cert_id = None
    if stability_certificate is not None:
        if stability_certificate.is_stale():
            failure_reasons.append("stability_certificate is stale; fixed-time claim denied")
        elif not stability_certificate.matches_metric(metric_identity):
            failure_reasons.append(
                "stability_certificate metric identity mismatch; fixed-time claim denied"
            )
        elif stability_runtime_config is None:
            # No runtime config provided: cannot verify certificate matches actual runtime.
            # Suppress fixed-time claim and downgrade to UNVERIFIED_CONVERGENCE.
            failure_reasons.append(
                "stability_runtime_config not provided; cannot verify certificate "
                "matches actual runtime recurrence — fixed-time claim denied"
            )
        elif not stability_certificate.matches_configuration(
            controller_id=stability_runtime_config.controller_id,
            controller_version=stability_runtime_config.controller_version,
            oscillation_policy_id=stability_runtime_config.oscillation_policy_id,
            discrete_update_interval_s=stability_runtime_config.discrete_update_interval_s,
            elfe_a=stability_runtime_config.elfe_a,
            elfe_b=stability_runtime_config.elfe_b,
            elfe_p=stability_runtime_config.elfe_p,
            elfe_q=stability_runtime_config.elfe_q,
            descent_scale=stability_runtime_config.descent_scale,
            perturbation_bound=stability_runtime_config.perturbation_bound,
            tolerance=stability_runtime_config.tolerance,
            max_steps=stability_runtime_config.max_steps,
            max_wall_time_s=stability_runtime_config.max_wall_time_s,
            admissible_initial_drift_max=stability_runtime_config.admissible_initial_drift_max,
            proof_artifact_id=stability_runtime_config.proof_artifact_id,
        ):
            failure_reasons.append(
                "stability_certificate.matches_configuration() failed; runtime recurrence "
                "does not match certificate assumptions — fixed-time claim denied"
            )
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
