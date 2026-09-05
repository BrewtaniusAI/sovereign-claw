"""Measured, evidence-bound closure authority.

All constructors are ordinary value objects except a verified binding: only
``ProofVault.verify_evidence_binding`` can mint that capability.  Possession of
a record hash, signature label, or zero scalar therefore conveys no authority.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence


_AUTHORITY_JSON_MAX_BYTES = 1_048_576
_AUTHORITY_JSON_MAX_DEPTH = 64


def _canonical_authority_json(value: Any) -> str:
    """Serialize bounded finite authority material without importing ProofVault.

    This intentionally matches ProofVault's canonical JSON representation for
    valid authority material while keeping the measurement value layer below
    the persistence layer in the import graph.
    """
    stack: list[tuple[Any, int]] = [(value, 0)]
    seen_ids: set[int] = set()
    while stack:
        node, depth = stack.pop()
        if depth > _AUTHORITY_JSON_MAX_DEPTH:
            raise ValueError("authority material exceeds maximum JSON depth")
        if isinstance(node, dict):
            node_id = id(node)
            if node_id in seen_ids:
                raise ValueError("authority material contains a cyclic reference")
            seen_ids.add(node_id)
            for key, item in node.items():
                if not isinstance(key, str):
                    raise ValueError("authority material mapping keys must be strings")
                stack.append((item, depth + 1))
        elif isinstance(node, (list, tuple)):
            node_id = id(node)
            if node_id in seen_ids:
                raise ValueError("authority material contains a cyclic reference")
            seen_ids.add(node_id)
            for item in node:
                stack.append((item, depth + 1))
        elif isinstance(node, float):
            if not math.isfinite(node):
                raise ValueError("authority material contains a non-finite float")
        elif not isinstance(node, (str, int, bool, type(None))):
            raise ValueError(
                f"authority material contains unsupported type {type(node).__name__!r}"
            )

    result = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    )
    if len(result.encode("utf-8")) > _AUTHORITY_JSON_MAX_BYTES:
        raise ValueError("authority material exceeds maximum canonical JSON size")
    return result


def authority_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_authority_json(value).encode()).hexdigest()


class MeasurementState(str, Enum):
    MEASURED = "MEASURED"
    UNMEASURED = "UNMEASURED"
    PREDICTED = "PREDICTED"


@dataclass(frozen=True)
class ComponentMeasurement:
    identity: str
    value: float | None
    state: MeasurementState
    evidence_record_hash: str | None
    trace_id: str
    observed_at: float

    def __post_init__(self) -> None:
        if self.state is MeasurementState.MEASURED and (
            self.value is None or not self.evidence_record_hash
        ):
            raise ValueError("MEASURED components require a value and evidence record")
        if self.state is not MeasurementState.MEASURED and self.value is not None:
            raise ValueError("unmeasured/predicted components cannot claim a measured value")

    def material(self) -> dict[str, Any]:
        value = asdict(self)
        value["state"] = self.state.value
        return value


@dataclass(frozen=True)
class ConstraintAssessment:
    trace_id: str
    step_id: str
    metric_identity: str
    evaluator_identity: str
    components: tuple[ComponentMeasurement, ...]
    violated_t_max: bool = False

    @property
    def assessment_hash(self) -> str:
        return authority_hash(self.material())

    def material(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "step_id": self.step_id,
            "metric_identity": self.metric_identity,
            "evaluator_identity": self.evaluator_identity,
            "components": [item.material() for item in self.components],
            "violated_t_max": self.violated_t_max,
        }


@dataclass(frozen=True)
class ClosureDecision:
    trace_id: str
    step_id: str
    assessment_hash: str
    metric_identity: str
    evaluator_identity: str
    status: str
    reasons: tuple[str, ...] = ()

    @property
    def decision_hash(self) -> str:
        material = asdict(self)
        material["reasons"] = list(self.reasons)
        return authority_hash(material)


@dataclass(frozen=True)
class VerifiedEvidenceBindingV1:
    """Unforgeable-by-construction process-local capability minted by ProofVault."""

    trace_id: str
    record_hash: str
    evidence_type: str
    provenance: str
    closure_decision_hash: str
    closure_status: str
    assessment_hash: str
    drift_metric_identity: str
    evaluator_identity: str
    step_id: str
    chain_tip_hash: str
    chain_verified_count: int
    _vault_token: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class VerifiedComponentEvidenceV1:
    trace_id: str
    assessment_hash: str
    record_hashes: frozenset[str]
    chain_tip_hash: str
    _vault_token: object = field(repr=False, compare=False)


@dataclass(frozen=True)
class StabilityCertificate:
    certificate_id: str
    metric_identity: str
    evaluator_identity: str
    runtime_build_identity: str
    coefficients: Mapping[str, float]
    assumptions: tuple[str, ...]
    domain: str
    version: str
    valid_from: float
    valid_until: float
    artifact_digest: str

    def material(self) -> dict[str, Any]:
        return {
            "certificate_id": self.certificate_id,
            "metric_identity": self.metric_identity,
            "evaluator_identity": self.evaluator_identity,
            "runtime_build_identity": self.runtime_build_identity,
            "coefficients": dict(self.coefficients),
            "assumptions": list(self.assumptions),
            "domain": self.domain,
            "version": self.version,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "artifact_digest": self.artifact_digest,
        }


class TrustedCertificateRegistry:
    """Server-owned immutable allow-list of exact certificate artifacts."""

    def __init__(self, approved_digests: Sequence[str], *, server_owned: bool = False) -> None:
        if not server_owned:
            raise PermissionError("certificate trust roots must be server-owned")
        self._approved = frozenset(approved_digests)

    def verify(
        self,
        certificate: StabilityCertificate,
        *,
        metric_identity: str,
        evaluator_identity: str,
        runtime_build_identity: str,
        now: float | None = None,
    ) -> bool:
        current = time.time() if now is None else now
        exact_digest = authority_hash(
            {k: v for k, v in certificate.material().items() if k != "artifact_digest"}
        )
        return bool(
            certificate.artifact_digest == exact_digest
            and exact_digest in self._approved
            and certificate.metric_identity == metric_identity
            and certificate.evaluator_identity == evaluator_identity
            and certificate.runtime_build_identity == runtime_build_identity
            and certificate.valid_from <= current <= certificate.valid_until
            and certificate.coefficients
            and certificate.assumptions
            and certificate.domain
            and certificate.version
        )


def implementation_digest(evaluator: Callable[..., Any]) -> str:
    target = getattr(evaluator, "__func__", evaluator)
    code = getattr(target, "__code__", None)
    if code is None:
        try:
            material = inspect.getsource(target).encode()
        except (OSError, TypeError):
            material = repr(type(target)).encode()
    else:
        material = b"|".join(
            (code.co_code, repr(code.co_consts).encode(), repr(code.co_names).encode())
        )
    return hashlib.sha256(material).hexdigest()


class EvaluatorRegistry:
    """Server-owned evaluator resolver with immediate implementation revalidation."""

    def __init__(self, *, server_owned: bool = False) -> None:
        if not server_owned:
            raise PermissionError("evaluator registry must be server-owned")
        self._entries: (
            dict[str, tuple[Callable[..., Any], str]] | Mapping[str, tuple[Callable[..., Any], str]]
        ) = {}
        self._frozen = False

    def register(self, identity: str, evaluator: Callable[..., Any]) -> None:
        if self._frozen:
            raise RuntimeError("evaluator registry is frozen")
        assert isinstance(self._entries, dict)
        self._entries[identity] = (evaluator, implementation_digest(evaluator))

    def freeze(self) -> None:
        self._entries = MappingProxyType(dict(self._entries))
        self._frozen = True

    def resolve(self, identity: str) -> Callable[..., Any]:
        if not self._frozen:
            raise RuntimeError("evaluator registry must be frozen before execution")
        evaluator, expected = self._entries[identity]
        if implementation_digest(evaluator) != expected:
            raise RuntimeError("evaluator implementation changed after registry freeze")
        return evaluator


REQUIRED_COMPONENTS = frozenset(
    {"constraint", "tool", "policy", "provider_uncertainty", "output", "postcondition"}
)


def evaluate_closure(
    assessment: ConstraintAssessment,
    *,
    verified_components: VerifiedComponentEvidenceV1,
    vault: Any,
) -> ClosureDecision:
    reasons: list[str] = []
    if verified_components._vault_token is not getattr(vault, "_binding_token", None):
        reasons.append("UNTRUSTED_COMPONENT_VERIFIER")
    if verified_components.trace_id != assessment.trace_id:
        reasons.append("VERIFIED_COMPONENT_TRACE_MISMATCH")
    if verified_components.assessment_hash != assessment.assessment_hash:
        reasons.append("VERIFIED_COMPONENT_ASSESSMENT_MISMATCH")
    by_identity = {component.identity: component for component in assessment.components}
    if assessment.violated_t_max:
        reasons.append("T_MAX_VIOLATION")
    if set(by_identity) != REQUIRED_COMPONENTS:
        reasons.append("REQUIRED_COMPONENT_SET_MISMATCH")
    for identity in sorted(REQUIRED_COMPONENTS):
        component = by_identity.get(identity)
        if component is None:
            continue
        if component.trace_id != assessment.trace_id:
            reasons.append(f"WRONG_TRACE:{identity}")
        if component.state is not MeasurementState.MEASURED:
            reasons.append(f"UNMEASURED:{identity}")
        elif component.evidence_record_hash not in verified_components.record_hashes:
            reasons.append(f"UNVERIFIED_COMPONENT:{identity}")
        elif component.value != 0.0:
            reasons.append(f"NONZERO:{identity}")
    return ClosureDecision(
        trace_id=assessment.trace_id,
        step_id=assessment.step_id,
        assessment_hash=assessment.assessment_hash,
        metric_identity=assessment.metric_identity,
        evaluator_identity=assessment.evaluator_identity,
        status="VERIFIED_CLOSURE" if not reasons else "NOT_CLOSED",
        reasons=tuple(reasons),
    )


__all__ = [
    "ClosureDecision",
    "ComponentMeasurement",
    "ConstraintAssessment",
    "EvaluatorRegistry",
    "MeasurementState",
    "REQUIRED_COMPONENTS",
    "StabilityCertificate",
    "TrustedCertificateRegistry",
    "VerifiedEvidenceBindingV1",
    "VerifiedComponentEvidenceV1",
    "authority_hash",
    "evaluate_closure",
    "implementation_digest",
]
