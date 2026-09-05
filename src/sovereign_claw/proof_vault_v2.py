"""
proof_vault_v2.py — Write-Time Tamper-Evident Evidence Ledger
==============================================================

Authoritative local evidence ledger for Sovereign Claw.

Local SQLite is tamper-evident, not physically immutable WORM.  The authority
boundary is a versioned append-only evidence chain with deterministic finite
JSON, explicit write transactions, durable metadata validation, and immutable
evidence/snapshot rows.  EventStream is a derived operator mirror only.
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

_DEFAULT_DB = Path.home() / ".sovereign_claw" / "proof_vault.sqlite3"
_ENV_DB = os.environ.get("SOVEREIGN_CLAW_DB")
DEFAULT_DB_PATH = Path(_ENV_DB) if _ENV_DB else _DEFAULT_DB

SCHEMA_VERSION = 2
HASH_VERSION = 1
GENESIS_PREV_HASH = "0" * 64

PROVENANCE_VERIFIED = "VERIFIED"
PROVENANCE_LEGACY = "LEGACY_UNVERIFIED"

_ALLOWED_PROVENANCE = {PROVENANCE_VERIFIED, PROVENANCE_LEGACY}

# Canonical-JSON payload bounds — centralized and versioned.
# Evidence payloads exceeding either limit are rejected before any
# projection/evidence/tip mutation so the ledger cannot be forced into
# unbounded serialization/hash memory.
MAX_CANONICAL_JSON_BYTES: int = 1_048_576  # 1 MiB UTF-8 canonical output
MAX_CANONICAL_JSON_DEPTH: int = 32  # maximum container nesting depth


def _validate_json_structure(obj: Any, max_depth: int = MAX_CANONICAL_JSON_DEPTH) -> None:
    """Non-recursively walk *obj* and reject:

    - container nesting deeper than *max_depth*;
    - non-string mapping keys;
    - non-finite floats (NaN / ±Inf);
    - unsupported value types (anything other than dict, list, str, int,
      float, bool, None);
    - cyclic references.

    Raises :class:`LedgerIntegrityError` on any violation.
    """
    seen_ids: set[int] = set()
    # Stack entries: (value, current_depth)
    stack: list[tuple[Any, int]] = [(obj, 0)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            raise LedgerIntegrityError(f"canonical JSON nesting depth exceeds limit ({max_depth})")
        if isinstance(node, dict):
            node_id = id(node)
            if node_id in seen_ids:
                raise LedgerIntegrityError("canonical JSON payload contains a cyclic reference")
            seen_ids.add(node_id)
            for k, v in node.items():
                if not isinstance(k, str):
                    raise LedgerIntegrityError(
                        f"canonical JSON mapping keys must be str, got {type(k).__name__!r}"
                    )
                stack.append((v, depth + 1))
        elif isinstance(node, list):
            node_id = id(node)
            if node_id in seen_ids:
                raise LedgerIntegrityError("canonical JSON payload contains a cyclic reference")
            seen_ids.add(node_id)
            for item in node:
                stack.append((item, depth + 1))
        elif isinstance(node, float):
            if not math.isfinite(node):
                raise LedgerIntegrityError(
                    f"canonical JSON payload contains non-finite float: {node!r}"
                )
        elif not isinstance(node, (str, int, bool, type(None))):
            raise LedgerIntegrityError(
                f"canonical JSON payload contains unsupported type {type(node).__name__!r}"
            )


def canonical_json(
    obj: Any,
    *,
    max_bytes: int = MAX_CANONICAL_JSON_BYTES,
    max_depth: int = MAX_CANONICAL_JSON_DEPTH,
) -> str:
    """Return deterministic, bounded, finite JSON suitable for authority hashing.

    Validates structure (depth, key types, cyclic references, non-finite floats,
    unsupported types) before serialization, then rejects output larger than
    *max_bytes* UTF-8 bytes.  Raises :class:`LedgerIntegrityError` on any
    violation so callers receive a clear, catchable error before any side effect.
    """
    _validate_json_structure(obj, max_depth=max_depth)
    result = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        ensure_ascii=False,
    )
    if len(result.encode("utf-8")) > max_bytes:
        raise LedgerIntegrityError(
            f"canonical JSON payload exceeds size limit "
            f"({len(result.encode('utf-8'))} > {max_bytes} bytes)"
        )
    return result


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp_us(value: float) -> int:
    if not math.isfinite(value):
        raise ValueError("timestamp must be finite")
    return int(round(value * 1_000_000))


def _record_authority_material(
    *,
    seq: int,
    evidence_type: str,
    trace_id: str,
    step_index: int | None,
    canonical_payload: str,
    timestamp: float,
    provenance: str,
    schema_version: int,
    hash_version: int,
) -> str:
    try:
        payload = json.loads(canonical_payload)
    except json.JSONDecodeError as exc:
        raise LedgerIntegrityError("canonical payload is not valid JSON") from exc
    normalized = canonical_json(payload)
    if normalized != canonical_payload:
        raise LedgerIntegrityError("canonical payload is not canonical JSON")
    return canonical_json(
        {
            "seq": seq,
            "schema_version": schema_version,
            "hash_version": hash_version,
            "provenance": provenance,
            "evidence_type": evidence_type,
            "trace_id": trace_id,
            "step_index": step_index,
            "timestamp_us": _timestamp_us(timestamp),
            "payload": payload,
        }
    )


def _hash_record_material(prev_hash: str, canonical_authority_material: str) -> str:
    return _sha256_text(prev_hash + canonical_authority_material)


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
    hash_version: int = HASH_VERSION


@dataclass
class ChainVerificationResult:
    ok: bool
    verified_count: int
    first_failure_seq: int | None
    failure_reason: str | None


class LedgerIntegrityError(RuntimeError):
    """Raised when authority evidence cannot be trusted or appended safely."""


class ProofVault:
    """SQLite-backed tamper-evident authority ledger."""

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        event_stream: EventStream | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        # Process-local verifier identity.  A caller can construct a value object
        # that resembles a binding, but cannot make it pass revalidation here.
        self._binding_token = object()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        event_log = os.environ.get("SOVEREIGN_CLAW_EVENT_LOG")
        self.event_stream = event_stream or (EventStream(Path(event_log)) if event_log else None)
        self._init_schema()
        self._migrate_legacy()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
            timeout=10,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    @contextmanager
    def _begin_immediate(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                # Rollback is best-effort during exception unwinding; the original
                # exception must be preserved and re-raised regardless.
                pass
            raise
        finally:
            conn.close()

    @staticmethod
    def _ensure_column(
        conn: sqlite3.Connection,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS traces (
                    trace_id   TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    objective  TEXT,
                    meta       JSON
                );

                CREATE TABLE IF NOT EXISTS steps (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id    TEXT NOT NULL,
                    step_index  INTEGER NOT NULL,
                    timestamp   REAL NOT NULL,
                    node        TEXT NOT NULL,
                    action      TEXT NOT NULL,
                    drift       REAL NOT NULL,
                    status      TEXT NOT NULL,
                    payload     JSON NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_steps_trace
                    ON steps(trace_id, step_index);

                CREATE TABLE IF NOT EXISTS agent_reputation (
                    agent_id         TEXT PRIMARY KEY,
                    drift_integral   REAL NOT NULL DEFAULT 0.0,
                    step_count       INTEGER NOT NULL DEFAULT 0,
                    last_updated     REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS evidence_records (
                    seq               INTEGER PRIMARY KEY,
                    evidence_type     TEXT NOT NULL,
                    trace_id          TEXT NOT NULL,
                    step_index        INTEGER,
                    canonical_payload TEXT NOT NULL,
                    prev_hash         TEXT NOT NULL,
                    record_hash       TEXT NOT NULL,
                    timestamp         REAL NOT NULL,
                    schema_version    INTEGER NOT NULL DEFAULT 2,
                    hash_version      INTEGER NOT NULL DEFAULT 1,
                    provenance        TEXT NOT NULL DEFAULT 'VERIFIED'
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_trace_step
                    ON evidence_records(trace_id, step_index)
                    WHERE step_index IS NOT NULL;

                CREATE INDEX IF NOT EXISTS idx_evidence_trace
                    ON evidence_records(trace_id, seq);

                CREATE TABLE IF NOT EXISTS legacy_trace_snapshots (
                    trace_id          TEXT PRIMARY KEY,
                    canonical_payload TEXT NOT NULL,
                    snapshot_hash     TEXT NOT NULL,
                    provenance        TEXT NOT NULL DEFAULT 'LEGACY_UNVERIFIED'
                );

                CREATE TABLE IF NOT EXISTS chain_meta (
                    id               INTEGER PRIMARY KEY CHECK (id = 1),
                    tip_seq          INTEGER NOT NULL DEFAULT 0,
                    tip_hash         TEXT NOT NULL DEFAULT '',
                    checkpoint_seq   INTEGER NOT NULL DEFAULT 0,
                    checkpoint_hash  TEXT NOT NULL DEFAULT '',
                    genesis_hash     TEXT NOT NULL DEFAULT '',
                    schema_version   INTEGER NOT NULL DEFAULT 2,
                    hash_version     INTEGER NOT NULL DEFAULT 1
                );

                CREATE TRIGGER IF NOT EXISTS trg_evidence_no_update
                BEFORE UPDATE ON evidence_records
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'evidence_records is immutable: UPDATE is forbidden'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS trg_evidence_no_delete
                BEFORE DELETE ON evidence_records
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'evidence_records is immutable: DELETE is forbidden'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS trg_legacy_trace_no_update
                BEFORE UPDATE ON legacy_trace_snapshots
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'legacy_trace_snapshots is immutable: UPDATE is forbidden'
                    );
                END;

                CREATE TRIGGER IF NOT EXISTS trg_legacy_trace_no_delete
                BEFORE DELETE ON legacy_trace_snapshots
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'legacy_trace_snapshots is immutable: DELETE is forbidden'
                    );
                END;
                """
            )

            self._ensure_column(
                conn,
                "evidence_records",
                "hash_version",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                conn,
                "chain_meta",
                "schema_version",
                f"INTEGER NOT NULL DEFAULT {SCHEMA_VERSION}",
            )
            self._ensure_column(
                conn,
                "chain_meta",
                "hash_version",
                f"INTEGER NOT NULL DEFAULT {HASH_VERSION}",
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO chain_meta(
                    id, tip_seq, tip_hash, checkpoint_seq, checkpoint_hash,
                    genesis_hash, schema_version, hash_version
                ) VALUES (1, 0, '', 0, '', '', ?, ?)
                """,
                (SCHEMA_VERSION, HASH_VERSION),
            )
        finally:
            conn.close()

    def _legacy_checkpoint_material(self, conn: sqlite3.Connection) -> str:
        trace_rows = conn.execute(
            """
            SELECT trace_id, canonical_payload, snapshot_hash, provenance
            FROM legacy_trace_snapshots
            ORDER BY trace_id ASC
            """
        ).fetchall()
        evidence_rows = conn.execute(
            """
            SELECT seq, evidence_type, trace_id, step_index, canonical_payload,
                   timestamp, schema_version, hash_version, provenance
            FROM evidence_records
            WHERE provenance = ?
            ORDER BY seq ASC
            """,
            (PROVENANCE_LEGACY,),
        ).fetchall()
        material = {
            "kind": "legacy-import-checkpoint-v1",
            "trace_snapshots": [
                {
                    "trace_id": r["trace_id"],
                    "canonical_payload": r["canonical_payload"],
                    "snapshot_hash": r["snapshot_hash"],
                    "provenance": r["provenance"],
                }
                for r in trace_rows
            ],
            "legacy_evidence": [
                {
                    "seq": r["seq"],
                    "evidence_type": r["evidence_type"],
                    "trace_id": r["trace_id"],
                    "step_index": r["step_index"],
                    "canonical_payload": r["canonical_payload"],
                    "timestamp_us": _timestamp_us(r["timestamp"]),
                    "schema_version": r["schema_version"],
                    "hash_version": r["hash_version"],
                    "provenance": r["provenance"],
                }
                for r in evidence_rows
            ],
        }
        return canonical_json(material)

    def _legacy_checkpoint(self, conn: sqlite3.Connection) -> tuple[int, str]:
        trace_count = conn.execute("SELECT COUNT(*) AS c FROM legacy_trace_snapshots").fetchone()[
            "c"
        ]
        legacy_rows = conn.execute(
            """
            SELECT seq FROM evidence_records
            WHERE provenance=?
            ORDER BY seq ASC
            """,
            (PROVENANCE_LEGACY,),
        ).fetchall()
        if not trace_count and not legacy_rows:
            return 0, ""
        checkpoint_seq = legacy_rows[-1]["seq"] if legacy_rows else 0
        return checkpoint_seq, _sha256_text(self._legacy_checkpoint_material(conn))

    def _migrate_legacy(self) -> None:
        with self._begin_immediate() as conn:
            verified = conn.execute(
                "SELECT COUNT(*) AS c FROM evidence_records WHERE provenance=?",
                (PROVENANCE_VERIFIED,),
            ).fetchone()["c"]

            if verified:
                unsupported = conn.execute(
                    """
                    SELECT seq FROM evidence_records
                    WHERE provenance=? AND (
                        schema_version != ? OR hash_version != ?
                    )
                    ORDER BY seq ASC LIMIT 1
                    """,
                    (PROVENANCE_VERIFIED, SCHEMA_VERSION, HASH_VERSION),
                ).fetchone()
                if unsupported is not None:
                    raise LedgerIntegrityError(
                        "pre-v2 VERIFIED ledger detected; explicit migration is required"
                    )
                return

            trace_rows = conn.execute(
                """
                SELECT trace_id, created_at, objective, meta
                FROM traces
                ORDER BY created_at ASC, trace_id ASC
                """
            ).fetchall()
            for row in trace_rows:
                existing = conn.execute(
                    "SELECT 1 FROM legacy_trace_snapshots WHERE trace_id=?",
                    (row["trace_id"],),
                ).fetchone()
                if existing is not None:
                    continue
                try:
                    meta = json.loads(row["meta"] or "{}")
                except json.JSONDecodeError:
                    meta = {"legacy_raw_meta": row["meta"]}
                payload = canonical_json(
                    {
                        "trace_id": row["trace_id"],
                        "created_at": row["created_at"],
                        "objective": row["objective"],
                        "meta": meta,
                    }
                )
                conn.execute(
                    """
                    INSERT INTO legacy_trace_snapshots(
                        trace_id, canonical_payload, snapshot_hash, provenance
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        row["trace_id"],
                        payload,
                        _sha256_text(payload),
                        PROVENANCE_LEGACY,
                    ),
                )

            step_rows = conn.execute(
                """
                SELECT s.id, s.trace_id, s.step_index, s.timestamp,
                       s.node, s.action, s.drift, s.status, s.payload
                FROM steps s
                LEFT JOIN evidence_records er
                  ON er.trace_id=s.trace_id AND er.step_index=s.step_index
                WHERE er.seq IS NULL
                ORDER BY s.id ASC
                """
            ).fetchall()
            next_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM evidence_records"
            ).fetchone()["n"]
            for row in step_rows:
                try:
                    payload_obj = json.loads(row["payload"])
                except json.JSONDecodeError:
                    payload_obj = {"legacy_raw_payload": row["payload"]}
                payload = canonical_json(
                    {
                        "node": row["node"],
                        "action": row["action"],
                        "drift": row["drift"],
                        "status": row["status"],
                        "payload": payload_obj,
                    }
                )
                conn.execute(
                    """
                    INSERT INTO evidence_records(
                        seq, evidence_type, trace_id, step_index,
                        canonical_payload, prev_hash, record_hash, timestamp,
                        schema_version, hash_version, provenance
                    ) VALUES (?, ?, ?, ?, ?, '', '', ?, ?, ?, ?)
                    """,
                    (
                        next_seq,
                        "step.legacy",
                        row["trace_id"],
                        row["step_index"],
                        payload,
                        row["timestamp"],
                        SCHEMA_VERSION,
                        HASH_VERSION,
                        PROVENANCE_LEGACY,
                    ),
                )
                next_seq += 1

            checkpoint_seq, checkpoint_hash = self._legacy_checkpoint(conn)
            conn.execute(
                """
                UPDATE chain_meta
                SET checkpoint_seq=?, checkpoint_hash=?,
                    tip_seq=0, tip_hash='', genesis_hash='',
                    schema_version=?, hash_version=?
                WHERE id=1
                """,
                (
                    checkpoint_seq,
                    checkpoint_hash,
                    SCHEMA_VERSION,
                    HASH_VERSION,
                ),
            )

    def _read_meta(self, conn: sqlite3.Connection) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM chain_meta WHERE id=1").fetchone()
        if row is None:
            raise LedgerIntegrityError("chain_meta sentinel row is missing")
        return row

    def _verification_failure(
        self,
        verified_count: int,
        seq: int | None,
        reason: str,
    ) -> ChainVerificationResult:
        return ChainVerificationResult(False, verified_count, seq, reason)

    def _verify_chain_conn(self, conn: sqlite3.Connection) -> ChainVerificationResult:
        meta = self._read_meta(conn)
        if meta["schema_version"] != SCHEMA_VERSION:
            return self._verification_failure(0, None, "unsupported chain metadata schema_version")
        if meta["hash_version"] != HASH_VERSION:
            return self._verification_failure(0, None, "unsupported chain metadata hash_version")

        rows = conn.execute(
            """
            SELECT seq, evidence_type, trace_id, step_index, canonical_payload,
                   prev_hash, record_hash, timestamp, schema_version,
                   hash_version, provenance
            FROM evidence_records
            ORDER BY seq ASC
            """
        ).fetchall()

        for expected, row in enumerate(rows, start=1):
            if row["seq"] != expected:
                return self._verification_failure(
                    0,
                    row["seq"],
                    f"global evidence sequence gap: expected {expected}, found {row['seq']}",
                )

        seen_verified = False
        verified_count = 0
        previous_hash = ""
        first_verified_hash = ""
        last_verified_seq = 0
        last_step_by_trace: dict[str, int] = {}

        for row in rows:
            seq = row["seq"]
            provenance = row["provenance"]
            if provenance not in _ALLOWED_PROVENANCE:
                return self._verification_failure(
                    verified_count, seq, f"unknown provenance {provenance!r}"
                )

            if provenance == PROVENANCE_LEGACY:
                if seen_verified:
                    return self._verification_failure(
                        verified_count,
                        seq,
                        "legacy evidence appears after VERIFIED chain start",
                    )
                if row["record_hash"] or row["prev_hash"]:
                    return self._verification_failure(
                        verified_count,
                        seq,
                        "legacy evidence must not claim write-time chain hashes",
                    )
                if row["step_index"] is not None:
                    last = last_step_by_trace.get(row["trace_id"])
                    last_step_by_trace[row["trace_id"]] = (
                        row["step_index"] if last is None else max(last, row["step_index"])
                    )
                continue

            seen_verified = True
            if row["schema_version"] != SCHEMA_VERSION:
                return self._verification_failure(
                    verified_count, seq, "unsupported evidence schema_version"
                )
            if row["hash_version"] != HASH_VERSION:
                return self._verification_failure(
                    verified_count, seq, "unsupported evidence hash_version"
                )
            if not row["evidence_type"] or not row["trace_id"]:
                return self._verification_failure(verified_count, seq, "empty evidence identity")
            if row["step_index"] is not None and row["step_index"] < 0:
                return self._verification_failure(verified_count, seq, "negative step_index")
            if row["evidence_type"].startswith("step.") and row["step_index"] is None:
                return self._verification_failure(
                    verified_count, seq, "step evidence missing step_index"
                )
            if row["evidence_type"] == "trace.created" and row["step_index"] is not None:
                return self._verification_failure(
                    verified_count, seq, "trace.created cannot carry step_index"
                )

            expected_prev = (
                meta["checkpoint_hash"]
                if verified_count == 0 and meta["checkpoint_hash"]
                else GENESIS_PREV_HASH
                if verified_count == 0
                else previous_hash
            )
            if row["prev_hash"] != expected_prev:
                return self._verification_failure(verified_count, seq, "prev_hash linkage mismatch")

            try:
                authority = _record_authority_material(
                    seq=seq,
                    evidence_type=row["evidence_type"],
                    trace_id=row["trace_id"],
                    step_index=row["step_index"],
                    canonical_payload=row["canonical_payload"],
                    timestamp=row["timestamp"],
                    provenance=row["provenance"],
                    schema_version=row["schema_version"],
                    hash_version=row["hash_version"],
                )
            except (ValueError, LedgerIntegrityError, TypeError) as exc:
                return self._verification_failure(
                    verified_count, seq, f"invalid canonical evidence: {exc}"
                )
            expected_hash = _hash_record_material(row["prev_hash"], authority)
            if expected_hash != row["record_hash"]:
                return self._verification_failure(verified_count, seq, "record_hash mismatch")

            if row["step_index"] is not None:
                last = last_step_by_trace.get(row["trace_id"])
                if last is not None and row["step_index"] <= last:
                    return self._verification_failure(
                        verified_count, seq, "step order violation in evidence history"
                    )
                last_step_by_trace[row["trace_id"]] = row["step_index"]

            if not first_verified_hash:
                first_verified_hash = row["record_hash"]
            previous_hash = row["record_hash"]
            last_verified_seq = seq
            verified_count += 1

        checkpoint_seq, checkpoint_hash = self._legacy_checkpoint(conn)
        if meta["checkpoint_seq"] != checkpoint_seq:
            return self._verification_failure(
                verified_count, None, "legacy checkpoint sequence mismatch"
            )
        if meta["checkpoint_hash"] != checkpoint_hash:
            return self._verification_failure(
                verified_count, None, "legacy checkpoint hash mismatch"
            )

        expected_tip_seq = last_verified_seq if verified_count else 0
        expected_tip_hash = previous_hash if verified_count else ""
        if meta["tip_seq"] != expected_tip_seq:
            return self._verification_failure(verified_count, None, "durable tip sequence mismatch")
        if meta["tip_hash"] != expected_tip_hash:
            return self._verification_failure(verified_count, None, "durable tip hash mismatch")
        if meta["genesis_hash"] != first_verified_hash:
            return self._verification_failure(verified_count, None, "durable genesis hash mismatch")

        snapshots = conn.execute(
            """
            SELECT trace_id, canonical_payload, snapshot_hash, provenance
            FROM legacy_trace_snapshots
            ORDER BY trace_id ASC
            """
        ).fetchall()
        for snap in snapshots:
            if snap["provenance"] != PROVENANCE_LEGACY:
                return self._verification_failure(
                    verified_count, None, "legacy trace snapshot provenance mismatch"
                )
            try:
                obj = json.loads(snap["canonical_payload"])
                if canonical_json(obj) != snap["canonical_payload"]:
                    raise ValueError("not canonical")
            except (json.JSONDecodeError, ValueError, TypeError):
                return self._verification_failure(
                    verified_count, None, "legacy trace snapshot is not canonical"
                )
            if _sha256_text(snap["canonical_payload"]) != snap["snapshot_hash"]:
                return self._verification_failure(
                    verified_count, None, "legacy trace snapshot hash mismatch"
                )

        return ChainVerificationResult(True, verified_count, None, None)

    def verify_chain(self) -> ChainVerificationResult:
        conn = self._connect()
        try:
            return self._verify_chain_conn(conn)
        finally:
            conn.close()

    def _require_healthy_chain_locked(self, conn: sqlite3.Connection) -> None:
        result = self._verify_chain_conn(conn)
        if not result.ok:
            raise LedgerIntegrityError(f"authority chain is not healthy: {result.failure_reason}")

    def _append_evidence_locked(
        self,
        conn: sqlite3.Connection,
        *,
        evidence_type: str,
        trace_id: str,
        step_index: int | None,
        payload: dict[str, Any],
        timestamp: float,
    ) -> EvidenceRecord:
        if not evidence_type or not trace_id:
            raise LedgerIntegrityError("evidence_type and trace_id are required")
        if step_index is not None and step_index < 0:
            raise LedgerIntegrityError("step_index must be non-negative")

        canonical_payload = canonical_json(payload)
        _timestamp_us(timestamp)

        self._require_healthy_chain_locked(conn)
        meta = self._read_meta(conn)
        next_seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM evidence_records"
        ).fetchone()["n"]

        prev_hash = (
            meta["tip_hash"]
            if meta["tip_seq"] > 0
            else meta["checkpoint_hash"]
            if meta["checkpoint_hash"]
            else GENESIS_PREV_HASH
        )

        authority = _record_authority_material(
            seq=next_seq,
            evidence_type=evidence_type,
            trace_id=trace_id,
            step_index=step_index,
            canonical_payload=canonical_payload,
            timestamp=timestamp,
            provenance=PROVENANCE_VERIFIED,
            schema_version=SCHEMA_VERSION,
            hash_version=HASH_VERSION,
        )
        record_hash = _hash_record_material(prev_hash, authority)

        conn.execute(
            """
            INSERT INTO evidence_records(
                seq, evidence_type, trace_id, step_index, canonical_payload,
                prev_hash, record_hash, timestamp, schema_version,
                hash_version, provenance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                next_seq,
                evidence_type,
                trace_id,
                step_index,
                canonical_payload,
                prev_hash,
                record_hash,
                timestamp,
                SCHEMA_VERSION,
                HASH_VERSION,
                PROVENANCE_VERIFIED,
            ),
        )

        if meta["tip_seq"] == 0:
            conn.execute(
                """
                UPDATE chain_meta
                SET tip_seq=?, tip_hash=?, genesis_hash=?,
                    schema_version=?, hash_version=?
                WHERE id=1
                """,
                (
                    next_seq,
                    record_hash,
                    record_hash,
                    SCHEMA_VERSION,
                    HASH_VERSION,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE chain_meta
                SET tip_seq=?, tip_hash=?, schema_version=?, hash_version=?
                WHERE id=1
                """,
                (
                    next_seq,
                    record_hash,
                    SCHEMA_VERSION,
                    HASH_VERSION,
                ),
            )

        return EvidenceRecord(
            seq=next_seq,
            evidence_type=evidence_type,
            trace_id=trace_id,
            step_index=step_index,
            canonical_payload=canonical_payload,
            prev_hash=prev_hash,
            record_hash=record_hash,
            timestamp=timestamp,
            schema_version=SCHEMA_VERSION,
            provenance=PROVENANCE_VERIFIED,
            hash_version=HASH_VERSION,
        )

    def _mirror(
        self,
        event_type: str,
        trace_id: str,
        ev: EvidenceRecord,
        payload: dict[str, Any],
    ) -> None:
        if self.event_stream is None:
            return
        try:
            self.event_stream.append(
                event_type,
                trace_id,
                {
                    **payload,
                    "source_seq": ev.seq,
                    "source_record_hash": ev.record_hash,
                },
            )
        except Exception:
            # EventStream is deliberately derived/best-effort; a mirror failure must
            # never become authority or roll back an already-committed ProofVault append.
            pass

    def create_trace(
        self,
        objective: str,
        meta: dict[str, Any] | None = None,
    ) -> str:
        trace_id = str(uuid.uuid4())
        meta_obj = {} if meta is None else meta
        created_at = time.time()
        payload = {
            "objective": objective,
            "meta": meta_obj,
            "created_at": created_at,
        }
        canonical_json(payload)

        with self._begin_immediate() as conn:
            self._require_healthy_chain_locked(conn)
            conn.execute(
                """
                INSERT INTO traces(trace_id, created_at, objective, meta)
                VALUES (?, ?, ?, ?)
                """,
                (
                    trace_id,
                    created_at,
                    objective,
                    canonical_json(meta_obj),
                ),
            )
            ev = self._append_evidence_locked(
                conn,
                evidence_type="trace.created",
                trace_id=trace_id,
                step_index=None,
                payload=payload,
                timestamp=created_at,
            )

        self._mirror("trace.created", trace_id, ev, payload)
        return trace_id

    def append_step(self, record: StepRecord) -> EvidenceRecord:
        if not math.isfinite(record.drift):
            raise LedgerIntegrityError(f"Non-finite drift value rejected: {record.drift!r}")
        if record.step_index < 0:
            raise LedgerIntegrityError("step_index must be non-negative")
        payload_str = canonical_json(record.payload)
        payload = {
            "node": record.node,
            "action": record.action,
            "drift": record.drift,
            "status": record.status,
            "payload": record.payload,
        }
        canonical_json(payload)
        _timestamp_us(record.timestamp)

        with self._begin_immediate() as conn:
            self._require_healthy_chain_locked(conn)
            trace_exists = conn.execute(
                "SELECT 1 FROM traces WHERE trace_id=?",
                (record.trace_id,),
            ).fetchone()
            if trace_exists is None:
                raise LedgerIntegrityError(f"unknown trace_id: {record.trace_id}")

            last = conn.execute(
                """
                SELECT MAX(step_index) AS m
                FROM evidence_records
                WHERE trace_id=? AND step_index IS NOT NULL
                """,
                (record.trace_id,),
            ).fetchone()["m"]
            if last is not None and record.step_index <= last:
                raise LedgerIntegrityError(
                    f"Step order violation: trace_id={record.trace_id!r} "
                    f"new step_index={record.step_index} must be > last={last}"
                )

            conn.execute(
                """
                INSERT INTO steps(
                    trace_id, step_index, timestamp, node, action,
                    drift, status, payload
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
            ev = self._append_evidence_locked(
                conn,
                evidence_type="step.appended",
                trace_id=record.trace_id,
                step_index=record.step_index,
                payload=payload,
                timestamp=record.timestamp,
            )

        self._mirror("step.appended", record.trace_id, ev, asdict(record))
        return ev

    def append_authority_event(
        self,
        event_type: str,
        trace_id: str,
        payload: dict[str, Any],
        *,
        timestamp: float | None = None,
    ) -> EvidenceRecord:
        """Append a non-step authority event for bridge/intent governance."""
        normalized = event_type.strip()
        if not normalized:
            raise LedgerIntegrityError("authority event_type is required")
        evidence_type = (
            normalized if normalized.startswith("authority.") else f"authority.{normalized}"
        )
        ts = time.time() if timestamp is None else timestamp
        canonical_json(payload)
        with self._begin_immediate() as conn:
            ev = self._append_evidence_locked(
                conn,
                evidence_type=evidence_type,
                trace_id=trace_id,
                step_index=None,
                payload=payload,
                timestamp=ts,
            )
        self._mirror(evidence_type, trace_id, ev, payload)
        return ev

    def get_evidence_records(self, trace_id: str | None = None) -> list[EvidenceRecord]:
        conn = self._connect()
        try:
            if trace_id is None:
                rows = conn.execute("SELECT * FROM evidence_records ORDER BY seq ASC").fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM evidence_records
                    WHERE trace_id=?
                    ORDER BY seq ASC
                    """,
                    (trace_id,),
                ).fetchall()
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
                hash_version=r["hash_version"],
            )
            for r in rows
        ]

    def get_evidence_record(self, seq: int) -> EvidenceRecord | None:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT * FROM evidence_records WHERE seq=?",
                (seq,),
            ).fetchone()
        finally:
            conn.close()
        if r is None:
            return None
        return EvidenceRecord(
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
            hash_version=r["hash_version"],
        )

    def verify_evidence_binding(
        self,
        record_hash: str,
        *,
        trace_id: str,
        evidence_type: str,
        closure_decision_hash: str,
        closure_status: str,
        assessment_hash: str,
        drift_metric_identity: str,
        evaluator_identity: str,
        step_id: str,
    ):  # type: ignore[no-untyped-def]
        """Verify exact closure membership and mint a process-local binding.

        The chain verification and membership read occur in one SQLite snapshot.
        Every semantic field is taken from and compared with the canonical stored
        payload; a plausible-looking hash or caller-created object is insufficient.
        """
        from .measured_closure import VerifiedEvidenceBindingV1

        expected = {
            "closure_decision_hash": closure_decision_hash,
            "closure_status": closure_status,
            "assessment_hash": assessment_hash,
            "drift_metric_identity": drift_metric_identity,
            "evaluator_identity": evaluator_identity,
            "step_id": step_id,
        }
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            verification = self._verify_chain_conn(conn)
            if not verification.ok:
                raise LedgerIntegrityError(
                    f"cannot bind evidence from unhealthy chain: {verification.failure_reason}"
                )
            rows = conn.execute(
                "SELECT * FROM evidence_records WHERE record_hash=?", (record_hash,)
            ).fetchall()
            if len(rows) != 1:
                raise LedgerIntegrityError("evidence hash is not a unique ProofVault member")
            row = rows[0]
            if row["provenance"] != PROVENANCE_VERIFIED:
                raise LedgerIntegrityError("legacy/unverified evidence cannot authorize closure")
            if row["schema_version"] != SCHEMA_VERSION or row["hash_version"] != HASH_VERSION:
                raise LedgerIntegrityError("unsupported evidence authority version")
            if row["trace_id"] != trace_id or row["evidence_type"] != evidence_type:
                raise LedgerIntegrityError("evidence trace or type mismatch")
            try:
                payload = json.loads(row["canonical_payload"])
            except json.JSONDecodeError as exc:
                raise LedgerIntegrityError("closure evidence payload is invalid") from exc
            if canonical_json(payload) != row["canonical_payload"]:
                raise LedgerIntegrityError("closure evidence payload is not canonical")
            if any(payload.get(key) != value for key, value in expected.items()):
                raise LedgerIntegrityError("closure evidence payload binding mismatch")
            meta = self._read_meta(conn)
            binding = VerifiedEvidenceBindingV1(
                trace_id=trace_id,
                record_hash=record_hash,
                evidence_type=evidence_type,
                provenance=row["provenance"],
                closure_decision_hash=closure_decision_hash,
                closure_status=closure_status,
                assessment_hash=assessment_hash,
                drift_metric_identity=drift_metric_identity,
                evaluator_identity=evaluator_identity,
                step_id=step_id,
                chain_tip_hash=meta["tip_hash"],
                chain_verified_count=verification.verified_count,
                _vault_token=self._binding_token,
            )
            conn.execute("COMMIT")
            return binding
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def revalidate_evidence_binding(self, binding: Any, decision: Any) -> bool:
        """Re-check a binding and exact decision immediately before actuation."""
        from .measured_closure import VerifiedEvidenceBindingV1

        if not isinstance(binding, VerifiedEvidenceBindingV1):
            raise LedgerIntegrityError("verified evidence binding is required")
        if binding._vault_token is not self._binding_token:
            raise LedgerIntegrityError("binding was not minted by this ProofVault instance")
        if decision.decision_hash != binding.closure_decision_hash:
            raise LedgerIntegrityError("closure decision does not match verified evidence")
        if decision.status != "VERIFIED_CLOSURE":
            raise LedgerIntegrityError("non-closure decision cannot authorize actuation")
        self.verify_evidence_binding(
            binding.record_hash,
            trace_id=decision.trace_id,
            evidence_type=binding.evidence_type,
            closure_decision_hash=decision.decision_hash,
            closure_status=decision.status,
            assessment_hash=decision.assessment_hash,
            drift_metric_identity=decision.metric_identity,
            evaluator_identity=decision.evaluator_identity,
            step_id=decision.step_id,
        )
        return True

    def verify_component_measurements(self, assessment: Any):  # type: ignore[no-untyped-def]
        """Verify every measured component against its exact persisted record."""
        from .measured_closure import MeasurementState, VerifiedComponentEvidenceV1

        conn = self._connect()
        try:
            conn.execute("BEGIN")
            verification = self._verify_chain_conn(conn)
            if not verification.ok:
                raise LedgerIntegrityError(
                    f"cannot verify components from unhealthy chain: {verification.failure_reason}"
                )
            hashes: set[str] = set()
            for component in assessment.components:
                if component.state is not MeasurementState.MEASURED:
                    continue
                rows = conn.execute(
                    "SELECT * FROM evidence_records WHERE record_hash=?",
                    (component.evidence_record_hash,),
                ).fetchall()
                if len(rows) != 1:
                    raise LedgerIntegrityError("component evidence is not a unique vault member")
                row = rows[0]
                if (
                    row["trace_id"] != assessment.trace_id
                    or row["provenance"] != PROVENANCE_VERIFIED
                    or row["evidence_type"] != "authority.component.measurement.v1"
                ):
                    raise LedgerIntegrityError("component evidence authority mismatch")
                payload = json.loads(row["canonical_payload"])
                if payload != {"identity": component.identity, "value": component.value}:
                    raise LedgerIntegrityError("component evidence payload mismatch")
                hashes.add(row["record_hash"])
            meta = self._read_meta(conn)
            result = VerifiedComponentEvidenceV1(
                trace_id=assessment.trace_id,
                assessment_hash=assessment.assessment_hash,
                record_hashes=frozenset(hashes),
                chain_tip_hash=meta["tip_hash"],
                _vault_token=self._binding_token,
            )
            conn.execute("COMMIT")
            return result
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_chain_tip(self) -> tuple[int, str]:
        conn = self._connect()
        try:
            meta = self._read_meta(conn)
            return meta["tip_seq"], meta["tip_hash"]
        finally:
            conn.close()

    def get_chain_metadata(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            meta = self._read_meta(conn)
            return {key: meta[key] for key in meta.keys()}
        finally:
            conn.close()

    def get_legacy_trace_snapshot(self, trace_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT canonical_payload, snapshot_hash, provenance
                FROM legacy_trace_snapshots
                WHERE trace_id=?
                """,
                (trace_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        try:
            payload = json.loads(row["canonical_payload"])
        except json.JSONDecodeError as exc:
            raise LedgerIntegrityError("legacy trace snapshot is corrupt") from exc
        return {
            "payload": payload,
            "snapshot_hash": row["snapshot_hash"],
            "provenance": row["provenance"],
        }

    def get_trace_steps(self, trace_id: str) -> list[StepRecord]:
        """Backward-compatible mutable projection reader (not authority)."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT trace_id, step_index, timestamp, node, action,
                       drift, status, payload
                FROM steps
                WHERE trace_id=?
                ORDER BY step_index ASC
                """,
                (trace_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            StepRecord(
                trace_id=r["trace_id"],
                step_index=r["step_index"],
                timestamp=r["timestamp"],
                node=r["node"],
                action=r["action"],
                drift=r["drift"],
                status=r["status"],
                payload=json.loads(r["payload"]),
            )
            for r in rows
        ]

    def get_trace_summary(self, trace_id: str) -> dict[str, Any]:
        """Compatibility summary; prefers evidence, falls back to legacy snapshot."""
        records = self.get_evidence_records(trace_id)
        objective = trace_id
        step_records: list[StepRecord] = []

        trace_event = next(
            (
                r
                for r in records
                if r.evidence_type == "trace.created" and r.provenance == PROVENANCE_VERIFIED
            ),
            None,
        )
        if trace_event is not None:
            payload = json.loads(trace_event.canonical_payload)
            objective = payload.get("objective", trace_id)
        else:
            legacy = self.get_legacy_trace_snapshot(trace_id)
            if legacy is not None:
                objective = legacy["payload"].get("objective", trace_id)

        for ev in records:
            if ev.step_index is None:
                continue
            try:
                payload = json.loads(ev.canonical_payload)
            except json.JSONDecodeError:
                continue
            step_records.append(
                StepRecord(
                    trace_id=ev.trace_id,
                    step_index=ev.step_index,
                    timestamp=ev.timestamp,
                    node=payload.get("node", ""),
                    action=payload.get("action", ""),
                    drift=float(payload.get("drift", 0.0)),
                    status=payload.get("status", ""),
                    payload=payload.get("payload", {}),
                )
            )

        if not step_records:
            step_records = self.get_trace_steps(trace_id)

        if not step_records:
            return {"trace_id": trace_id, "objective": objective, "steps": 0}

        drifts = [s.drift for s in step_records]
        return {
            "trace_id": trace_id,
            "objective": objective,
            "steps": len(step_records),
            "initial_drift": drifts[0],
            "final_drift": drifts[-1],
            "min_drift": min(drifts),
            "final_status": step_records[-1].status,
            "converged": step_records[-1].drift == 0.0,
        }

    def update_agent_reputation(self, agent_id: str, step_drift: float) -> None:
        if not math.isfinite(step_drift):
            raise ValueError("step_drift must be finite")
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO agent_reputation(
                    agent_id, drift_integral, step_count, last_updated
                ) VALUES (?, ?, 1, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    drift_integral=drift_integral + excluded.drift_integral,
                    step_count=step_count + 1,
                    last_updated=excluded.last_updated
                """,
                (agent_id, step_drift, now),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                # Rollback is best-effort during exception unwinding; the original
                # exception must be preserved and re-raised regardless.
                pass
            raise
        finally:
            conn.close()

    def get_agent_reputation_weight(self, agent_id: str, k: float = 1.0) -> float:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT drift_integral FROM agent_reputation WHERE agent_id=?",
                (agent_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return 1.0
        return math.exp(-k * row["drift_integral"])

    def list_agent_weights(self, k: float = 1.0) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT agent_id, drift_integral, step_count
                FROM agent_reputation
                ORDER BY drift_integral ASC
                """
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


__all__ = [
    "DEFAULT_DB_PATH",
    "SCHEMA_VERSION",
    "HASH_VERSION",
    "GENESIS_PREV_HASH",
    "PROVENANCE_VERIFIED",
    "PROVENANCE_LEGACY",
    "MAX_CANONICAL_JSON_BYTES",
    "MAX_CANONICAL_JSON_DEPTH",
    "canonical_json",
    "StepRecord",
    "TraceRecord",
    "EvidenceRecord",
    "ChainVerificationResult",
    "LedgerIntegrityError",
    "ProofVault",
]
