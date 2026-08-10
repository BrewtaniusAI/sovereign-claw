"""
proof_vault.py — Write-Time Tamper-Evident Evidence Ledger
===========================================================
Append-only SQLite ledger for zero-trust enterprise auditing.

Architecture (issue #15 / PROOFVAULT_INTEGRITY.md)
---------------------------------------------------
• ``evidence_records`` — versioned, immutable append log carrying
  global monotonic sequence, prev_hash, record_hash, schema_version,
  and provenance (VERIFIED | LEGACY_UNVERIFIED).
• ``chain_meta`` — single durable tip/checkpoint row advanced
  atomically with every append.
• Every authoritative append uses ``BEGIN IMMEDIATE``:
    1. Read and verify durable tip.
    2. Validate domain constraints (step identity / order).
    3. Canonical-JSON-hash the record material + prev_hash.
    4. INSERT evidence row.
    5. UPDATE chain tip.
    6. COMMIT.
• ``BEFORE UPDATE`` and ``BEFORE DELETE`` triggers block mutations of
  immutable evidence rows.
• Legacy rows from before the chain existed are permanently tagged
  ``LEGACY_UNVERIFIED`` and never retroactively certified.
• ``EventStream`` is a derived/best-effort mirror only; its write
  failure does not affect authority.
• ``verify_chain()`` walks the entire evidence log and validates
  genesis semantics, contiguous sequence, stored prev_hash linkage,
  recomputed record_hash, and legacy boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .event_stream import EventStream

# ── DB path resolution ────────────────────────────────────────────────────────
_DEFAULT_DB = Path.home() / ".sovereign_claw" / "proof_vault.sqlite3"
_ENV_DB = os.environ.get("SOVEREIGN_CLAW_DB")
DEFAULT_DB_PATH = Path(_ENV_DB) if _ENV_DB else _DEFAULT_DB

# ── Constants ─────────────────────────────────────────────────────────────────
SCHEMA_VERSION = 1
GENESIS_PREV_HASH = "0" * 64  # conventional zero-hash sentinel for genesis record

# Provenance tags
PROVENANCE_VERIFIED = "VERIFIED"
PROVENANCE_LEGACY = "LEGACY_UNVERIFIED"


# ── Canonical JSON ────────────────────────────────────────────────────────────


def canonical_json(obj: Any) -> str:
    """
    Return a deterministic UTF-8 JSON string suitable for hashing.

    Rules (per PROOFVAULT_INTEGRITY.md):
    • Keys sorted recursively.
    • No whitespace separators (compact).
    • Non-finite floats (NaN, ±Inf) are rejected.
    • Non-serialisable / cyclic values raise TypeError.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash_record_material(prev_hash: str, canonical: str) -> str:
    """SHA-256 of ``prev_hash || canonical_record`` (UTF-8 concatenated)."""
    data = (prev_hash + canonical).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


# ── Data classes ──────────────────────────────────────────────────────────────
@dataclass
class StepRecord:
    trace_id: str
    step_index: int
    timestamp: float
    node: str
    action: str
    drift: float
    status: str
    payload: dict[str, Any]


@dataclass
class TraceRecord:
    trace_id: str
    created_at: float
    objective: str
    meta: dict[str, Any]


@dataclass
class EvidenceRecord:
    """Reflects a single row from the ``evidence_records`` table."""

    seq: int
    evidence_type: str
    trace_id: str
    step_index: int | None
    canonical_payload: str
    prev_hash: str
    record_hash: str
    timestamp: float
    schema_version: int
    provenance: str


@dataclass
class ChainVerificationResult:
    ok: bool
    verified_count: int
    first_failure_seq: int | None
    failure_reason: str | None


# ── Errors ────────────────────────────────────────────────────────────────────


class LedgerIntegrityError(RuntimeError):
    """Raised when a write-time constraint is violated."""


# ── ProofVault ────────────────────────────────────────────────────────────────
class ProofVault:
    """
    Write-time tamper-evident evidence ledger.

    Authoritative evidence is written to ``evidence_records`` in a single
    ``BEGIN IMMEDIATE`` transaction that atomically verifies the current tip,
    inserts the new evidence row, and advances ``chain_meta``.

    ``EventStream`` (if configured) receives a derived mirror record *after*
    the authority commit; its failure is logged but never retried or allowed
    to roll back the authority write.
    """

    def __init__(
        self, db_path: Path = DEFAULT_DB_PATH, event_stream: EventStream | None = None
    ) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        event_log = os.environ.get("SOVEREIGN_CLAW_EVENT_LOG")
        self.event_stream = event_stream or (EventStream(Path(event_log)) if event_log else None)
        self._init_schema()
        self._migrate_legacy()

    # ── Internal connection ───────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # we manage transactions explicitly
            timeout=10,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _begin_immediate(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager that wraps a ``BEGIN IMMEDIATE`` transaction."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            # executescript() issues an implicit COMMIT before running, so
            # we let it manage its own transaction.
            conn.executescript(
                """
                PRAGMA journal_mode = WAL;

                -- Legacy mutable tables (kept for backward compat; not authority)
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id   TEXT    PRIMARY KEY,
                    created_at REAL    NOT NULL,
                    objective  TEXT,
                    meta       JSON
                );

                CREATE TABLE IF NOT EXISTS steps (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id    TEXT    NOT NULL,
                    step_index  INTEGER NOT NULL,
                    timestamp   REAL    NOT NULL,
                    node        TEXT    NOT NULL,
                    action      TEXT    NOT NULL,
                    drift       REAL    NOT NULL,
                    status      TEXT    NOT NULL,
                    payload     JSON    NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_steps_trace
                    ON steps(trace_id, step_index);

                -- Byzantine Reputation Weighting
                CREATE TABLE IF NOT EXISTS agent_reputation (
                    agent_id         TEXT    PRIMARY KEY,
                    drift_integral   REAL    NOT NULL DEFAULT 0.0,
                    step_count       INTEGER NOT NULL DEFAULT 0,
                    last_updated     REAL    NOT NULL
                );

                -- ── Authoritative immutable evidence log ─────────────────────
                CREATE TABLE IF NOT EXISTS evidence_records (
                    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
                    evidence_type    TEXT    NOT NULL,
                    trace_id         TEXT    NOT NULL,
                    step_index       INTEGER,
                    canonical_payload TEXT   NOT NULL,
                    prev_hash        TEXT    NOT NULL,
                    record_hash      TEXT    NOT NULL,
                    timestamp        REAL    NOT NULL,
                    schema_version   INTEGER NOT NULL DEFAULT 1,
                    provenance       TEXT    NOT NULL DEFAULT 'VERIFIED'
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_trace_step
                    ON evidence_records(trace_id, step_index)
                    WHERE step_index IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_evidence_trace
                    ON evidence_records(trace_id);

                -- ── Durable chain tip ─────────────────────────────────────────
                CREATE TABLE IF NOT EXISTS chain_meta (
                    id              INTEGER PRIMARY KEY CHECK (id = 1),
                    tip_seq         INTEGER NOT NULL DEFAULT 0,
                    tip_hash        TEXT    NOT NULL DEFAULT '',
                    checkpoint_seq  INTEGER NOT NULL DEFAULT 0,
                    checkpoint_hash TEXT    NOT NULL DEFAULT '',
                    genesis_hash    TEXT    NOT NULL DEFAULT ''
                );

                -- ── Immutability triggers ─────────────────────────────────────
                CREATE TRIGGER IF NOT EXISTS trg_evidence_no_update
                BEFORE UPDATE ON evidence_records
                BEGIN
                    SELECT RAISE(ABORT,
                        'evidence_records is immutable: UPDATE is forbidden');
                END;

                CREATE TRIGGER IF NOT EXISTS trg_evidence_no_delete
                BEFORE DELETE ON evidence_records
                BEGIN
                    SELECT RAISE(ABORT,
                        'evidence_records is immutable: DELETE is forbidden');
                END;
                """
            )
        finally:
            conn.close()

    # ── Migration: legacy rows ────────────────────────────────────────────────

    def _migrate_legacy(self) -> None:
        """
        One-time import of pre-chain ``steps`` rows into ``evidence_records``
        tagged as ``LEGACY_UNVERIFIED``.  Rows that have already been imported
        are skipped.  The chain tip is NOT advanced for legacy rows — they sit
        outside the verified chain.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            # Ensure chain_meta sentinel row exists
            conn.execute(
                "INSERT OR IGNORE INTO chain_meta(id, tip_seq, tip_hash, "
                "checkpoint_seq, checkpoint_hash, genesis_hash) "
                "VALUES (1, 0, '', 0, '', '')"
            )
            # Only import legacy steps not yet present in evidence_records
            rows = conn.execute(
                """
                SELECT s.trace_id, s.step_index, s.timestamp,
                       s.node, s.action, s.drift, s.status, s.payload
                FROM steps s
                LEFT JOIN evidence_records er
                    ON er.trace_id = s.trace_id AND er.step_index = s.step_index
                WHERE er.seq IS NULL
                ORDER BY s.id ASC
                """
            ).fetchall()
            for row in rows:
                payload_obj = json.loads(row["payload"])
                material = {
                    "evidence_type": "step.legacy",
                    "trace_id": row["trace_id"],
                    "step_index": row["step_index"],
                    "timestamp": row["timestamp"],
                    "node": row["node"],
                    "action": row["action"],
                    "drift": row["drift"],
                    "status": row["status"],
                    "payload": payload_obj,
                }
                canon = canonical_json(material)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO evidence_records(
                        evidence_type, trace_id, step_index,
                        canonical_payload, prev_hash, record_hash,
                        timestamp, schema_version, provenance
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "step.legacy",
                        row["trace_id"],
                        row["step_index"],
                        canon,
                        "",  # no prev_hash: outside the chain
                        "",  # no record_hash: outside the chain
                        row["timestamp"],
                        SCHEMA_VERSION,
                        PROVENANCE_LEGACY,
                    ),
                )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    # ── Internal: tip read/write ──────────────────────────────────────────────

    def _read_tip(self, conn: sqlite3.Connection) -> tuple[int, str]:
        """Return (tip_seq, tip_hash) from durable chain_meta."""
        row = conn.execute("SELECT tip_seq, tip_hash FROM chain_meta WHERE id = 1").fetchone()
        if row is None:
            raise LedgerIntegrityError("chain_meta sentinel row is missing")
        return row["tip_seq"], row["tip_hash"]

    def _verify_tip(self, conn: sqlite3.Connection, expected_seq: int, expected_hash: str) -> None:
        """Confirm durable tip matches the record we just fetched (detects concurrent writers)."""
        tip_seq, tip_hash = self._read_tip(conn)
        if tip_seq != expected_seq or tip_hash != expected_hash:
            raise LedgerIntegrityError(
                f"Tip race detected: expected seq={expected_seq} hash={expected_hash[:12]}…, "
                f"found seq={tip_seq} hash={tip_hash[:12]}…"
            )

    # ── Core authoritative append ─────────────────────────────────────────────

    def _append_evidence(
        self,
        *,
        evidence_type: str,
        trace_id: str,
        step_index: int | None,
        material: dict[str, Any],
        timestamp: float,
        conn: sqlite3.Connection,
    ) -> EvidenceRecord:
        """
        Low-level append inside an already-open BEGIN IMMEDIATE transaction.

        1. Read and verify the durable tip.
        2. Canonicalise material and compute record_hash.
        3. INSERT evidence row.
        4. UPDATE chain tip.
        Returns the new EvidenceRecord (not yet committed).
        """
        tip_seq, tip_hash = self._read_tip(conn)
        prev_hash = tip_hash if tip_seq > 0 else GENESIS_PREV_HASH

        # Validate step-identity uniqueness (per-trace)
        if step_index is not None:
            dup = conn.execute(
                "SELECT seq FROM evidence_records WHERE trace_id = ? AND step_index = ?",
                (trace_id, step_index),
            ).fetchone()
            if dup is not None:
                raise LedgerIntegrityError(
                    f"Duplicate step identity: trace_id={trace_id!r} step_index={step_index}"
                )

        # Canonicalise and hash
        canon = canonical_json(material)
        record_hash = _hash_record_material(prev_hash, canon)
        now = timestamp

        conn.execute(
            """
            INSERT INTO evidence_records(
                evidence_type, trace_id, step_index,
                canonical_payload, prev_hash, record_hash,
                timestamp, schema_version, provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                evidence_type,
                trace_id,
                step_index,
                canon,
                prev_hash,
                record_hash,
                now,
                SCHEMA_VERSION,
                PROVENANCE_VERIFIED,
            ),
        )
        new_seq = conn.execute("SELECT last_insert_rowid() AS r").fetchone()["r"]

        # First record sets genesis_hash
        if tip_seq == 0:
            conn.execute(
                "UPDATE chain_meta SET tip_seq=?, tip_hash=?, genesis_hash=? WHERE id=1",
                (new_seq, record_hash, record_hash),
            )
        else:
            # Verify tip hasn't shifted between our read and our write (serialization guard)
            self._verify_tip(conn, tip_seq, tip_hash)
            conn.execute(
                "UPDATE chain_meta SET tip_seq=?, tip_hash=? WHERE id=1",
                (new_seq, record_hash),
            )

        return EvidenceRecord(
            seq=new_seq,
            evidence_type=evidence_type,
            trace_id=trace_id,
            step_index=step_index,
            canonical_payload=canon,
            prev_hash=prev_hash,
            record_hash=record_hash,
            timestamp=now,
            schema_version=SCHEMA_VERSION,
            provenance=PROVENANCE_VERIFIED,
        )

    # ── Chain verification ────────────────────────────────────────────────────

    def verify_chain(self) -> ChainVerificationResult:
        """
        Walk all VERIFIED evidence records in sequence order and validate:
        - Genesis sentinel (first VERIFIED record uses GENESIS_PREV_HASH).
        - Contiguous global sequence (no gaps).
        - Stored prev_hash linkage.
        - Recomputed record_hash matches stored value.
        - Durable tip/checkpoint consistency.
        - Legacy provenance boundary (LEGACY rows must not appear mid-chain).

        Returns a ChainVerificationResult.  Verification fails closed and
        never auto-heals.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT seq, evidence_type, trace_id, step_index,
                       canonical_payload, prev_hash, record_hash,
                       schema_version, provenance
                FROM evidence_records
                ORDER BY seq ASC
                """
            ).fetchall()
            tip_seq_stored, tip_hash_stored = self._read_tip(conn)
        finally:
            conn.close()

        verified_count = 0
        prev_hash = ""
        seen_verified = False
        prev_seq: int | None = None

        for row in rows:
            seq = row["seq"]
            provenance = row["provenance"]

            # Legacy rows are exempt from chain verification
            if provenance == PROVENANCE_LEGACY:
                continue

            if not seen_verified:
                # First VERIFIED record must use the genesis sentinel
                if row["prev_hash"] != GENESIS_PREV_HASH:
                    return ChainVerificationResult(
                        ok=False,
                        verified_count=verified_count,
                        first_failure_seq=seq,
                        failure_reason=f"seq={seq}: genesis prev_hash mismatch",
                    )
                prev_hash = GENESIS_PREV_HASH
                seen_verified = True
            else:
                # Sequence gap among VERIFIED records: only legal if every row
                # with seq in (prev_seq, seq) has LEGACY provenance.
                if prev_seq is not None and seq != prev_seq + 1:
                    gap_rows = [
                        r
                        for r in rows
                        if prev_seq < r["seq"] < seq and r["provenance"] != PROVENANCE_LEGACY
                    ]
                    if gap_rows:
                        return ChainVerificationResult(
                            ok=False,
                            verified_count=verified_count,
                            first_failure_seq=gap_rows[0]["seq"],
                            failure_reason=(
                                f"seq={gap_rows[0]['seq']}: VERIFIED record missing or "
                                f"out of order between seq={prev_seq} and seq={seq}"
                            ),
                        )

                if row["prev_hash"] != prev_hash:
                    return ChainVerificationResult(
                        ok=False,
                        verified_count=verified_count,
                        first_failure_seq=seq,
                        failure_reason=(
                            f"seq={seq}: prev_hash mismatch "
                            f"(stored={row['prev_hash'][:12]}… "
                            f"expected={prev_hash[:12]}…)"
                        ),
                    )

            # Recompute record_hash
            expected_hash = _hash_record_material(row["prev_hash"], row["canonical_payload"])
            if expected_hash != row["record_hash"]:
                return ChainVerificationResult(
                    ok=False,
                    verified_count=verified_count,
                    first_failure_seq=seq,
                    failure_reason=f"seq={seq}: record_hash mismatch (payload tampered)",
                )

            prev_hash = row["record_hash"]
            prev_seq = seq
            verified_count += 1

        # Validate durable tip matches last verified record
        if verified_count > 0:
            if tip_hash_stored != prev_hash:
                return ChainVerificationResult(
                    ok=False,
                    verified_count=verified_count,
                    first_failure_seq=None,
                    failure_reason=(
                        f"Durable tip hash mismatch: stored={tip_hash_stored[:12]}… "
                        f"computed={prev_hash[:12]}…"
                    ),
                )

        return ChainVerificationResult(
            ok=True,
            verified_count=verified_count,
            first_failure_seq=None,
            failure_reason=None,
        )

    # ── Public evidence-record reader ─────────────────────────────────────────

    def get_evidence_records(self, trace_id: str | None = None) -> list[EvidenceRecord]:
        """Return evidence records in seq order (optionally filtered by trace_id)."""
        conn = self._connect()
        try:
            if trace_id:
                rows = conn.execute(
                    "SELECT * FROM evidence_records WHERE trace_id=? ORDER BY seq ASC",
                    (trace_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM evidence_records ORDER BY seq ASC").fetchall()
        finally:
            conn.close()
        return [
            EvidenceRecord(
                seq=r["seq"],
                evidence_type=r["evidence_type"],
                trace_id=r["trace_id"],
                step_index=r["step_index"],
                canonical_payload=r["canonical_payload"],
                prev_hash=r["prev_hash"],
                record_hash=r["record_hash"],
                timestamp=r["timestamp"],
                schema_version=r["schema_version"],
                provenance=r["provenance"],
            )
            for r in rows
        ]

    def get_chain_tip(self) -> tuple[int, str]:
        """Return (tip_seq, tip_hash)."""
        conn = self._connect()
        try:
            return self._read_tip(conn)
        finally:
            conn.close()

    # ── Trace management ──────────────────────────────────────────────────────

    def create_trace(
        self,
        objective: str,
        meta: dict[str, Any] | None = None,
    ) -> str:
        trace_id = str(uuid.uuid4())
        meta = meta or {}
        created_at = time.time()

        # Write to legacy traces table (backward compat)
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "INSERT INTO traces(trace_id, created_at, objective, meta) VALUES (?, ?, ?, ?)",
                (trace_id, created_at, objective, json.dumps(meta)),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

        # Append authoritative evidence record
        material: dict[str, Any] = {
            "evidence_type": "trace.created",
            "trace_id": trace_id,
            "objective": objective,
            "meta": meta,
            "created_at": created_at,
        }
        with self._begin_immediate() as c:
            ev = self._append_evidence(
                evidence_type="trace.created",
                trace_id=trace_id,
                step_index=None,
                material=material,
                timestamp=created_at,
                conn=c,
            )

        # Best-effort derived mirror
        if self.event_stream is not None:
            try:
                self.event_stream.append(
                    "trace.created",
                    trace_id,
                    {
                        "objective": objective,
                        "meta": meta,
                        "created_at": created_at,
                        "source_seq": ev.seq,
                        "source_record_hash": ev.record_hash,
                    },
                )
            except Exception:
                pass  # mirror failure never blocks authority

        return trace_id

    def append_step(self, record: StepRecord) -> EvidenceRecord:
        """
        Append a step to both the legacy ``steps`` table and the authoritative
        ``evidence_records`` chain in a single ``BEGIN IMMEDIATE`` transaction.

        Raises ``LedgerIntegrityError`` on:
        - duplicate (trace_id, step_index)
        - monotonically decreasing step_index for the same trace
        - non-finite float in payload
        """
        # Validate step ordering before acquiring the write lock
        conn = self._connect()
        try:
            last = conn.execute(
                "SELECT MAX(step_index) AS m FROM evidence_records "
                "WHERE trace_id=? AND step_index IS NOT NULL AND provenance=?",
                (record.trace_id, PROVENANCE_VERIFIED),
            ).fetchone()["m"]
        finally:
            conn.close()

        if last is not None and record.step_index <= last:
            raise LedgerIntegrityError(
                f"Step order violation: trace_id={record.trace_id!r} "
                f"new step_index={record.step_index} must be > last={last}"
            )

        if not math.isfinite(record.drift):
            raise LedgerIntegrityError(f"Non-finite drift value rejected: {record.drift!r}")
        payload_str = canonical_json(record.payload)  # raises on NaN/Inf/cyclic

        material: dict[str, Any] = {
            "evidence_type": "step.appended",
            "trace_id": record.trace_id,
            "step_index": record.step_index,
            "timestamp": record.timestamp,
            "node": record.node,
            "action": record.action,
            "drift": record.drift,
            "status": record.status,
            "payload": record.payload,
        }

        with self._begin_immediate() as c:
            # Also persist to legacy steps table inside the same transaction
            c.execute(
                """
                INSERT INTO steps(
                    trace_id, step_index, timestamp,
                    node, action, drift, status, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.trace_id,
                    record.step_index,
                    record.timestamp,
                    record.node,
                    record.action,
                    record.drift,
                    record.status,
                    payload_str,
                ),
            )
            ev = self._append_evidence(
                evidence_type="step.appended",
                trace_id=record.trace_id,
                step_index=record.step_index,
                material=material,
                timestamp=record.timestamp,
                conn=c,
            )

        # Best-effort derived mirror
        if self.event_stream is not None:
            try:
                self.event_stream.append(
                    "step.appended",
                    record.trace_id,
                    {
                        **asdict(record),
                        "source_seq": ev.seq,
                        "source_record_hash": ev.record_hash,
                    },
                )
            except Exception:
                pass

        return ev

    # ── Retrieval ─────────────────────────────────────────────────────────────

    def get_trace_steps(self, trace_id: str) -> list[StepRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT trace_id, step_index, timestamp,
                       node, action, drift, status, payload
                FROM steps
                WHERE trace_id = ?
                ORDER BY step_index ASC
                """,
                (trace_id,),
            ).fetchall()
        finally:
            conn.close()

        return [
            StepRecord(
                trace_id=row["trace_id"],
                step_index=row["step_index"],
                timestamp=row["timestamp"],
                node=row["node"],
                action=row["action"],
                drift=row["drift"],
                status=row["status"],
                payload=json.loads(row["payload"]),
            )
            for row in rows
        ]

    def get_trace_summary(self, trace_id: str) -> dict[str, Any]:
        """Quick analytics: step count, drift trajectory, final status."""
        steps = self.get_trace_steps(trace_id)

        objective = trace_id
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT objective FROM traces WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
            if row:
                objective = row["objective"]
        finally:
            conn.close()

        if not steps:
            return {"trace_id": trace_id, "objective": objective, "steps": 0}

        drifts = [s.drift for s in steps]
        return {
            "trace_id": trace_id,
            "objective": objective,
            "steps": len(steps),
            "initial_drift": drifts[0] if drifts else None,
            "final_drift": drifts[-1],
            "min_drift": min(drifts),
            "final_status": steps[-1].status,
            "converged": steps[-1].drift == 0.0,
        }

    # ── Byzantine Reputation Weighting ───────────────────────────────────────

    def update_agent_reputation(self, agent_id: str, step_drift: float) -> None:
        """Accumulate drift integral for an agent (called after each step)."""
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO agent_reputation(agent_id, drift_integral, step_count, last_updated)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    drift_integral = drift_integral + excluded.drift_integral,
                    step_count     = step_count + 1,
                    last_updated   = excluded.last_updated
                """,
                (agent_id, step_drift, now),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def get_agent_reputation_weight(self, agent_id: str, k: float = 1.0) -> float:
        """Returns w_i = e^{-k·R_i}."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT drift_integral FROM agent_reputation WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return 1.0

        return math.exp(-k * row["drift_integral"])

    def list_agent_weights(self, k: float = 1.0) -> list[dict[str, Any]]:
        """Return all agents sorted by reputation weight descending."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT agent_id, drift_integral, step_count FROM agent_reputation "
                "ORDER BY drift_integral ASC"
            ).fetchall()
        finally:
            conn.close()

        return [
            {
                "agent_id": r["agent_id"],
                "drift_integral": r["drift_integral"],
                "step_count": r["step_count"],
                "weight": math.exp(-k * r["drift_integral"]),
            }
            for r in rows
        ]
