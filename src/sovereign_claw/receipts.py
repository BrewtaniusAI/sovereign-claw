"""
receipts.py — Proof Receipt Export, Verified Replay, and Diff
=============================================================

Receipts consume write-time ProofVault evidence.  A receipt does not invent a
new authority chain at export time.  VERIFIED receipts are bound to exact
stored evidence records and to a global ledger checkpoint/tip that existed
when the receipt was built.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from .proof_vault import (
    GENESIS_PREV_HASH,
    PROVENANCE_LEGACY,
    PROVENANCE_VERIFIED,
    LedgerIntegrityError,
    ProofVault,
    canonical_json,
)


@dataclass
class HashedStep:
    """Receipt view of a stored step evidence record."""

    step_index: int
    node: str
    action: str
    drift: float
    status: str
    payload: dict[str, Any]
    timestamp: float
    step_hash: str = ""
    prev_hash: str = ""
    evidence_seq: int = 0

    def compute_hash(self, prev: str = "") -> str:
        """
        Compatibility helper for non-authoritative diagnostics.

        ProofVault ``record_hash`` is the authority.  This helper is retained
        for callers that used the old per-step digest API; it never upgrades
        legacy/unverified data to VERIFIED.
        """
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
        digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
        self.prev_hash = prev
        self.step_hash = digest
        return digest


@dataclass
class ProofReceipt:
    trace_id: str
    objective: str
    steps: list[HashedStep]
    chain_root: str = ""
    chain_tip: str = ""
    total_steps: int = 0
    final_drift: float = 0.0
    final_status: str = ""
    verified: bool = False
    provenance: str = PROVENANCE_VERIFIED
    ledger_tip_seq: int = 0
    ledger_tip_hash: str = ""
    ledger_anchor_hash: str = ""
    ledger_genesis_hash: str = ""
    trace_evidence_seqs: list[int] = field(default_factory=list)


@dataclass
class ReplayStep:
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
    step_index: int
    field: str
    trace_a_value: Any
    trace_b_value: Any


@dataclass
class TraceDiff:
    trace_a_id: str
    trace_b_id: str
    common_steps: int
    only_in_a: int
    only_in_b: int
    differences: list[DiffEntry] = field(default_factory=list)
    drift_a_final: float = 0.0
    drift_b_final: float = 0.0


class ReceiptBuilder:
    """Build and verify receipts against stored ProofVault evidence."""

    def __init__(self, vault: ProofVault) -> None:
        self._vault = vault

    @staticmethod
    def _payload(ev: Any) -> dict[str, Any]:
        try:
            payload = json.loads(ev.canonical_payload)
        except (json.JSONDecodeError, TypeError) as exc:
            raise LedgerIntegrityError(
                f"evidence seq={ev.seq} contains invalid canonical payload"
            ) from exc
        if canonical_json(payload) != ev.canonical_payload:
            raise LedgerIntegrityError(f"evidence seq={ev.seq} payload is not canonical")
        if not isinstance(payload, dict):
            raise LedgerIntegrityError(f"evidence seq={ev.seq} payload must be an object")
        return payload

    def _objective_from_evidence(
        self,
        trace_id: str,
        records: list[Any],
    ) -> tuple[str, bool]:
        for ev in records:
            if ev.evidence_type == "trace.created" and ev.provenance == PROVENANCE_VERIFIED:
                payload = self._payload(ev)
                return str(payload.get("objective", trace_id)), False
        snapshot = self._vault.get_legacy_trace_snapshot(trace_id)
        if snapshot is not None:
            return str(snapshot["payload"].get("objective", trace_id)), True
        return trace_id, False

    def _build_step(self, ev: Any) -> HashedStep:
        payload = self._payload(ev)
        return HashedStep(
            step_index=int(ev.step_index),
            node=str(payload.get("node", "")),
            action=str(payload.get("action", "")),
            drift=float(payload.get("drift", 0.0)),
            status=str(payload.get("status", "")),
            payload=payload.get("payload", {}) or {},
            timestamp=float(ev.timestamp),
            step_hash=ev.record_hash,
            prev_hash=ev.prev_hash,
            evidence_seq=ev.seq,
        )

    def build_receipt(self, trace_id: str) -> ProofReceipt:
        records = self._vault.get_evidence_records(trace_id=trace_id)
        objective, legacy_snapshot = self._objective_from_evidence(trace_id, records)

        has_legacy = legacy_snapshot or any(r.provenance == PROVENANCE_LEGACY for r in records)
        provenance = PROVENANCE_LEGACY if has_legacy else PROVENANCE_VERIFIED

        steps: list[HashedStep] = []
        for ev in records:
            if ev.step_index is None:
                continue
            steps.append(self._build_step(ev))

        meta = self._vault.get_chain_metadata()
        ledger_tip_seq = int(meta["tip_seq"])
        ledger_tip_hash = str(meta["tip_hash"])
        ledger_anchor_hash = (
            str(meta["checkpoint_hash"]) if meta["checkpoint_hash"] else GENESIS_PREV_HASH
        )
        ledger_genesis_hash = str(meta["genesis_hash"])

        receipt = ProofReceipt(
            trace_id=trace_id,
            objective=objective,
            steps=steps,
            chain_root=ledger_genesis_hash,
            chain_tip=ledger_tip_hash,
            total_steps=len(steps),
            final_drift=steps[-1].drift if steps else 0.0,
            final_status=steps[-1].status if steps else "",
            verified=False,
            provenance=provenance,
            ledger_tip_seq=ledger_tip_seq,
            ledger_tip_hash=ledger_tip_hash,
            ledger_anchor_hash=ledger_anchor_hash,
            ledger_genesis_hash=ledger_genesis_hash,
            trace_evidence_seqs=[r.seq for r in records],
        )
        receipt.verified = self.verify_chain(receipt)
        return receipt

    def verify_chain(self, receipt: ProofReceipt) -> bool:
        """
        Verify the receipt itself against the stored authority ledger.

        A healthy vault is necessary but not sufficient: declared ledger
        anchors, exact evidence membership, and every step field/hash are
        checked.  A mutated receipt cannot verify merely because the database
        is healthy.
        """
        if receipt.provenance == PROVENANCE_LEGACY:
            return False

        chain_result = self._vault.verify_chain()
        if not chain_result.ok:
            return False

        meta = self._vault.get_chain_metadata()
        expected_root = str(meta["genesis_hash"])
        expected_anchor = (
            str(meta["checkpoint_hash"]) if meta["checkpoint_hash"] else GENESIS_PREV_HASH
        )

        if receipt.chain_root != receipt.ledger_genesis_hash:
            return False
        if receipt.ledger_genesis_hash != expected_root:
            return False
        if receipt.ledger_anchor_hash != expected_anchor:
            return False
        if receipt.chain_tip != receipt.ledger_tip_hash:
            return False
        if receipt.total_steps != len(receipt.steps):
            return False

        if receipt.ledger_tip_seq == 0:
            if receipt.ledger_tip_hash:
                return False
        else:
            tip_record = self._vault.get_evidence_record(receipt.ledger_tip_seq)
            if tip_record is None:
                return False
            if tip_record.provenance != PROVENANCE_VERIFIED:
                return False
            if tip_record.record_hash != receipt.ledger_tip_hash:
                return False
            current_tip_seq = int(meta["tip_seq"])
            if current_tip_seq < receipt.ledger_tip_seq:
                return False

        all_records = self._vault.get_evidence_records()
        current_records = {r.seq: r for r in all_records}
        if len(set(receipt.trace_evidence_seqs)) != len(receipt.trace_evidence_seqs):
            return False
        expected_trace_seqs = [
            r.seq
            for r in all_records
            if r.trace_id == receipt.trace_id and r.seq <= receipt.ledger_tip_seq
        ]
        if receipt.trace_evidence_seqs != expected_trace_seqs:
            return False

        referenced = []
        for seq in receipt.trace_evidence_seqs:
            ev = current_records.get(seq)
            if ev is None or ev.trace_id != receipt.trace_id:
                return False
            if ev.provenance != PROVENANCE_VERIFIED:
                return False
            referenced.append(ev)

        trace_created = next(
            (r for r in referenced if r.evidence_type == "trace.created"),
            None,
        )
        if trace_created is None:
            return False
        try:
            trace_payload = self._payload(trace_created)
        except LedgerIntegrityError:
            return False
        if str(trace_payload.get("objective", receipt.trace_id)) != receipt.objective:
            return False

        step_by_seq = {s.evidence_seq: s for s in receipt.steps}
        referenced_steps = [r for r in referenced if r.step_index is not None]
        if set(step_by_seq) != {r.seq for r in referenced_steps}:
            return False

        for ev in referenced_steps:
            step = step_by_seq[ev.seq]
            if step.step_hash != ev.record_hash or step.prev_hash != ev.prev_hash:
                return False
            if step.step_index != ev.step_index:
                return False
            try:
                payload = self._payload(ev)
            except LedgerIntegrityError:
                return False
            expected_payload = payload.get("payload", {}) or {}
            if (
                step.node != str(payload.get("node", ""))
                or step.action != str(payload.get("action", ""))
                or step.drift != float(payload.get("drift", 0.0))
                or step.status != str(payload.get("status", ""))
                or step.payload != expected_payload
                or step.timestamp != float(ev.timestamp)
            ):
                return False

        if receipt.steps:
            if receipt.final_drift != receipt.steps[-1].drift:
                return False
            if receipt.final_status != receipt.steps[-1].status:
                return False
        else:
            if receipt.final_drift != 0.0 or receipt.final_status != "":
                return False

        return True

    def export(
        self,
        trace_id: str,
        fmt: Literal["json", "hash"] = "json",
    ) -> str:
        receipt = self.build_receipt(trace_id)
        if fmt == "hash":
            lines = [
                f"trace_id: {receipt.trace_id}",
                f"chain_root: {receipt.chain_root}",
                f"chain_tip: {receipt.chain_tip}",
                f"ledger_tip_seq: {receipt.ledger_tip_seq}",
                f"ledger_anchor_hash: {receipt.ledger_anchor_hash}",
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
                    f"  [{step.step_index}] seq={step.evidence_seq} "
                    f"{step.step_hash} (prev={step.prev_hash[:12]}...)"
                )
            return "\n".join(lines)

        return json.dumps(
            {
                "trace_id": receipt.trace_id,
                "objective": receipt.objective,
                "chain_root": receipt.chain_root,
                "chain_tip": receipt.chain_tip,
                "ledger_tip_seq": receipt.ledger_tip_seq,
                "ledger_tip_hash": receipt.ledger_tip_hash,
                "ledger_anchor_hash": receipt.ledger_anchor_hash,
                "ledger_genesis_hash": receipt.ledger_genesis_hash,
                "trace_evidence_seqs": receipt.trace_evidence_seqs,
                "total_steps": receipt.total_steps,
                "final_drift": receipt.final_drift,
                "final_status": receipt.final_status,
                "verified": receipt.verified,
                "provenance": receipt.provenance,
                "steps": [
                    {
                        "evidence_seq": s.evidence_seq,
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
        """
        Produce verified replay from stored authority evidence.

        Projection-only or legacy/tampered history is deliberately rejected.
        """
        receipt = self.build_receipt(trace_id)
        if not receipt.verified or not self.verify_chain(receipt):
            raise LedgerIntegrityError(
                "verified replay unavailable: trace evidence is legacy, tampered, "
                "or not bound to the current authority ledger"
            )

        replay_steps: list[ReplayStep] = []
        prev_drift = 1.0
        for step in receipt.steps:
            payload = step.payload or {}
            replay_steps.append(
                ReplayStep(
                    step_index=step.step_index,
                    node=step.node,
                    action=step.action,
                    drift=step.drift,
                    drift_delta=step.drift - prev_drift,
                    status=step.status,
                    tool=str(payload.get("tool", step.action)),
                    success=bool(payload.get("success", True)),
                    comment=str(
                        payload.get(
                            "decision_comment",
                            payload.get("reason", ""),
                        )
                    ),
                )
            )
            prev_drift = step.drift
        return replay_steps

    def diff(self, trace_a_id: str, trace_b_id: str) -> TraceDiff:
        receipt_a = self.build_receipt(trace_a_id)
        receipt_b = self.build_receipt(trace_b_id)
        if not receipt_a.verified or not receipt_b.verified:
            raise LedgerIntegrityError("verified diff requires VERIFIED evidence for both traces")

        steps_a = receipt_a.steps
        steps_b = receipt_b.steps
        common = min(len(steps_a), len(steps_b))
        differences: list[DiffEntry] = []

        for i in range(common):
            a, b = steps_a[i], steps_b[i]
            for field_name in ("node", "action", "drift", "status"):
                va = getattr(a, field_name)
                vb = getattr(b, field_name)
                if va != vb:
                    differences.append(
                        DiffEntry(
                            step_index=a.step_index,
                            field=field_name,
                            trace_a_value=va,
                            trace_b_value=vb,
                        )
                    )

        return TraceDiff(
            trace_a_id=trace_a_id,
            trace_b_id=trace_b_id,
            common_steps=common,
            only_in_a=max(0, len(steps_a) - common),
            only_in_b=max(0, len(steps_b) - common),
            differences=differences,
            drift_a_final=steps_a[-1].drift if steps_a else 0.0,
            drift_b_final=steps_b[-1].drift if steps_b else 0.0,
        )


__all__ = [
    "HashedStep",
    "ProofReceipt",
    "ReplayStep",
    "DiffEntry",
    "TraceDiff",
    "ReceiptBuilder",
]
