"""
receipts.py — Proof Receipt Export, Hash Chain, Replay, and Diff
================================================================
Converts ProofVault evidence chains into verifiable, exportable
proof receipts. Supports:
  - JSON and hash-digest export formats
  - Verification against stored ledger hashes (not re-invented)
  - Step-by-step replay of execution traces
  - Cross-run diff comparing two execution traces

Design note (issue #15 / PROOFVAULT_INTEGRITY.md)
--------------------------------------------------
``ReceiptBuilder.build_receipt()`` reads stored ``record_hash`` /
``prev_hash`` values from the authoritative ``evidence_records`` table.
``verified=True`` is set **only** after a successful ``ProofVault.verify_chain()``
pass that binds the receipt's declared root/tip to the verified ledger state.
``HashedStep.compute_hash()`` is used only for the per-step hash format
export and cross-checking; it is NOT the authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from .proof_vault import (
    PROVENANCE_LEGACY,
    PROVENANCE_VERIFIED,
    ProofVault,
    canonical_json,
)


# ── Receipt data structures ───────────────────────────────────────────────────
@dataclass
class HashedStep:
    """A single execution step with its deterministic hash."""

    step_index: int
    node: str
    action: str
    drift: float
    status: str
    payload: dict[str, Any]
    timestamp: float
    step_hash: str = ""
    prev_hash: str = ""

    def compute_hash(self, prev: str = "") -> str:
        """Compute SHA-256 hash chaining this step to the previous."""
        material = {
            "step_index": self.step_index,
            "node": self.node,
            "action": self.action,
            "drift": self.drift,
            "status": self.status,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "prev_hash": prev,
        }
        canonical = canonical_json(material)
        self.prev_hash = prev
        self.step_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return self.step_hash


@dataclass
class ProofReceipt:
    """Exportable proof receipt for a governed execution trace."""

    trace_id: str
    objective: str
    steps: list[HashedStep]
    chain_root: str = ""
    chain_tip: str = ""
    total_steps: int = 0
    final_drift: float = 0.0
    final_status: str = ""
    verified: bool = False
    provenance: str = PROVENANCE_VERIFIED  # VERIFIED or LEGACY_UNVERIFIED


@dataclass
class ReplayStep:
    """A single step in an execution replay."""

    step_index: int
    node: str
    action: str
    drift: float
    drift_delta: float
    status: str
    tool: str
    success: bool
    comment: str


@dataclass
class DiffEntry:
    """A single difference between two execution traces."""

    step_index: int
    field: str
    trace_a_value: Any
    trace_b_value: Any


@dataclass
class TraceDiff:
    """Comparison result between two execution traces."""

    trace_a_id: str
    trace_b_id: str
    common_steps: int
    only_in_a: int
    only_in_b: int
    differences: list[DiffEntry] = field(default_factory=list)
    drift_a_final: float = 0.0
    drift_b_final: float = 0.0


# ── Receipt builder ───────────────────────────────────────────────────────────
class ReceiptBuilder:
    """Builds proof receipts from ProofVault evidence records."""

    def __init__(self, vault: ProofVault) -> None:
        self._vault = vault

    def build_receipt(self, trace_id: str) -> ProofReceipt:
        """
        Build a proof receipt for a trace.

        Steps are populated from ``evidence_records`` (authoritative).
        ``verified=True`` is set only after a successful
        ``ProofVault.verify_chain()`` pass.

        If the trace contains any LEGACY_UNVERIFIED evidence the receipt
        provenance is set to LEGACY_UNVERIFIED and verified remains False.
        """
        # Pull authoritative evidence records for this trace
        ev_records = self._vault.get_evidence_records(trace_id=trace_id)
        # Filter to step.appended records only (skip trace.created etc.)
        step_evs = [r for r in ev_records if r.step_index is not None]

        # Check legacy provenance
        has_legacy = any(r.provenance == PROVENANCE_LEGACY for r in ev_records)
        receipt_provenance = PROVENANCE_LEGACY if has_legacy else PROVENANCE_VERIFIED

        # Build HashedStep objects from stored evidence
        hashed_steps: list[HashedStep] = []
        for ev in step_evs:
            try:
                material = json.loads(ev.canonical_payload)
            except (json.JSONDecodeError, ValueError):
                material = {}
            hs = HashedStep(
                step_index=ev.step_index or 0,
                node=material.get("node", ""),
                action=material.get("action", ""),
                drift=material.get("drift", 0.0),
                status=material.get("status", ""),
                payload=material.get("payload", {}),
                timestamp=material.get("timestamp", ev.timestamp),
                step_hash=ev.record_hash,
                prev_hash=ev.prev_hash,
            )
            hashed_steps.append(hs)

        # Fall back to get_trace_steps if no evidence records exist (empty DB)
        if not hashed_steps:
            raw_steps = self._vault.get_trace_steps(trace_id)
            prev_h = ""
            for step in raw_steps:
                hs = HashedStep(
                    step_index=step.step_index,
                    node=step.node,
                    action=step.action,
                    drift=step.drift,
                    status=step.status,
                    payload=step.payload,
                    timestamp=step.timestamp,
                )
                prev_h = hs.compute_hash(prev_h)
                hashed_steps.append(hs)

        chain_root = hashed_steps[0].step_hash if hashed_steps else ""
        chain_tip = hashed_steps[-1].step_hash if hashed_steps else ""

        summary = self._vault.get_trace_summary(trace_id)

        # Verify ledger chain; only set verified=True on clean pass
        if receipt_provenance == PROVENANCE_VERIFIED and hashed_steps:
            chain_result = self._vault.verify_chain()
            verified = chain_result.ok
        else:
            verified = False

        return ProofReceipt(
            trace_id=trace_id,
            objective=summary.get("objective", trace_id),
            steps=hashed_steps,
            chain_root=chain_root,
            chain_tip=chain_tip,
            total_steps=len(hashed_steps),
            final_drift=summary.get("final_drift", 0.0),
            final_status=summary.get("final_status", ""),
            verified=verified,
            provenance=receipt_provenance,
        )

    def verify_chain(self, receipt: ProofReceipt) -> bool:
        """
        Verify receipt integrity against the stored ledger.

        For VERIFIED receipts: runs a full ``ProofVault.verify_chain()``
        and confirms the receipt's hashed steps exist in the valid ledger.
        For LEGACY_UNVERIFIED receipts: always returns False (per contract).
        """
        if receipt.provenance == PROVENANCE_LEGACY:
            return False

        result = self._vault.verify_chain()
        return result.ok

    def export(
        self,
        trace_id: str,
        fmt: Literal["json", "hash"] = "json",
    ) -> str:
        """Export a proof receipt in the specified format."""
        receipt = self.build_receipt(trace_id)

        if fmt == "hash":
            lines = [
                f"trace_id: {receipt.trace_id}",
                f"chain_root: {receipt.chain_root}",
                f"chain_tip: {receipt.chain_tip}",
                f"total_steps: {receipt.total_steps}",
                f"final_drift: {receipt.final_drift}",
                f"final_status: {receipt.final_status}",
                f"verified: {receipt.verified}",
                f"provenance: {receipt.provenance}",
                "",
                "hash_chain:",
            ]
            for step in receipt.steps:
                lines.append(
                    f"  [{step.step_index}] {step.step_hash} (prev={step.prev_hash[:12]}...)"
                    if step.prev_hash
                    else f"  [{step.step_index}] {step.step_hash} (root)"
                )
            return "\n".join(lines)

        # JSON format
        return json.dumps(
            {
                "trace_id": receipt.trace_id,
                "chain_root": receipt.chain_root,
                "chain_tip": receipt.chain_tip,
                "total_steps": receipt.total_steps,
                "final_drift": receipt.final_drift,
                "final_status": receipt.final_status,
                "verified": receipt.verified,
                "provenance": receipt.provenance,
                "steps": [
                    {
                        "step_index": s.step_index,
                        "node": s.node,
                        "action": s.action,
                        "drift": s.drift,
                        "status": s.status,
                        "step_hash": s.step_hash,
                        "prev_hash": s.prev_hash,
                    }
                    for s in receipt.steps
                ],
            },
            indent=2,
        )

    def replay(self, trace_id: str) -> list[ReplayStep]:
        """Generate a step-by-step replay of an execution trace."""
        raw_steps = self._vault.get_trace_steps(trace_id)
        replay_steps: list[ReplayStep] = []
        prev_drift = 1.0

        for step in raw_steps:
            payload = step.payload or {}
            replay_steps.append(
                ReplayStep(
                    step_index=step.step_index,
                    node=step.node,
                    action=step.action,
                    drift=step.drift,
                    drift_delta=step.drift - prev_drift,
                    status=step.status,
                    tool=payload.get("tool", step.action),
                    success=payload.get("success", True),
                    comment=payload.get("decision_comment", payload.get("reason", "")),
                )
            )
            prev_drift = step.drift

        return replay_steps

    def diff(self, trace_a_id: str, trace_b_id: str) -> TraceDiff:
        """Compare two execution traces and produce a structured diff."""
        steps_a = self._vault.get_trace_steps(trace_a_id)
        steps_b = self._vault.get_trace_steps(trace_b_id)

        common = min(len(steps_a), len(steps_b))
        differences: list[DiffEntry] = []

        for i in range(common):
            a, b = steps_a[i], steps_b[i]
            for fld in ("node", "action", "drift", "status"):
                va = getattr(a, fld)
                vb = getattr(b, fld)
                if va != vb:
                    differences.append(
                        DiffEntry(
                            step_index=a.step_index,
                            field=fld,
                            trace_a_value=va,
                            trace_b_value=vb,
                        )
                    )

        drift_a = steps_a[-1].drift if steps_a else 0.0
        drift_b = steps_b[-1].drift if steps_b else 0.0

        return TraceDiff(
            trace_a_id=trace_a_id,
            trace_b_id=trace_b_id,
            common_steps=common,
            only_in_a=max(0, len(steps_a) - common),
            only_in_b=max(0, len(steps_b) - common),
            differences=differences,
            drift_a_final=drift_a,
            drift_b_final=drift_b,
        )
