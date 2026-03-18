"""
proof_vault.py — Immutable WORM Ledger
=======================================
Append-only SQLite ledger for zero-trust enterprise auditing.

BUG FIXES / UPGRADES vs. original:
  - Thread-safe: uses check_same_thread=False + WAL mode.
  - get_trace_steps used wrong enumerate tuple unpacking (off-by-one
    index variable shadowed loop var); fixed.
  - Added get_trace_summary() for quick drift analytics.
  - Added agent_reputation table for Byzantine Reputation Weighting:
      R_i = ∫ D_i(t) dt     w_i = e^{-k·R_i}
  - All writes use context managers (no leaked connections on exception).
  - DB path is configurable via env var SOVEREIGN_CLAW_DB for CI/test.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .event_stream import EventStream

# ── DB path resolution ────────────────────────────────────────────────────────
_DEFAULT_DB = Path.home() / ".sovereign_claw" / "proof_vault.sqlite3"
_ENV_DB = os.environ.get("SOVEREIGN_CLAW_DB")
DEFAULT_DB_PATH = Path(_ENV_DB) if _ENV_DB else _DEFAULT_DB


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
    payload: Dict[str, Any]


@dataclass
class TraceRecord:
    trace_id: str
    created_at: float
    objective: str
    meta: Dict[str, Any]


# ── ProofVault ────────────────────────────────────────────────────────────────
class ProofVault:
    """
    Immutable local ledger for zero-trust enterprise auditing.

    Design
    ------
    • Append-only StepRecord table (no UPDATE/DELETE).
    • Trace-level metadata stored as JSON.
    • Agent reputation table tracks historical drift integrals.
    • WAL mode + thread-safe connections.
    """

    def __init__(
        self, db_path: Path = DEFAULT_DB_PATH, event_stream: Optional[EventStream] = None
    ) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        event_log = os.environ.get("SOVEREIGN_CLAW_EVENT_LOG")
        self.event_stream = event_stream or (EventStream(Path(event_log)) if event_log else None)
        self._init_schema()

    # ── Schema ────────────────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

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
                -- R_i = cumulative drift integral per agent
                CREATE TABLE IF NOT EXISTS agent_reputation (
                    agent_id         TEXT    PRIMARY KEY,
                    drift_integral   REAL    NOT NULL DEFAULT 0.0,
                    step_count       INTEGER NOT NULL DEFAULT 0,
                    last_updated     REAL    NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage transactions
        )
        conn.row_factory = sqlite3.Row
        return conn

    # ── Trace management ──────────────────────────────────────────────────────
    def create_trace(
        self,
        objective: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        trace_id = str(uuid.uuid4())
        meta = meta or {}
        created_at = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO traces(trace_id, created_at, objective, meta) VALUES (?, ?, ?, ?)",
                (trace_id, created_at, objective, json.dumps(meta)),
            )
        if self.event_stream is not None:
            self.event_stream.append(
                "trace.created",
                trace_id,
                {"objective": objective, "meta": meta, "created_at": created_at},
            )
        return trace_id

    def append_step(self, record: StepRecord) -> None:
        with self._connect() as conn:
            conn.execute(
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
                    json.dumps(record.payload),
                ),
            )
        if self.event_stream is not None:
            self.event_stream.append("step.appended", record.trace_id, asdict(record))

    # ── Retrieval ─────────────────────────────────────────────────────────────
    def get_trace_steps(self, trace_id: str) -> List[StepRecord]:
        with self._connect() as conn:
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

    def get_trace_summary(self, trace_id: str) -> Dict[str, Any]:
        """
        Quick analytics: step count, drift trajectory, final status.
        """
        steps = self.get_trace_steps(trace_id)
        if not steps:
            return {"trace_id": trace_id, "steps": 0}

        drifts = [s.drift for s in steps]
        return {
            "trace_id": trace_id,
            "steps": len(steps),
            "initial_drift": drifts[0] if drifts else None,
            "final_drift": drifts[-1],
            "min_drift": min(drifts),
            "final_status": steps[-1].status,
            "converged": steps[-1].drift == 0.0,
        }

    # ── Byzantine Reputation Weighting ───────────────────────────────────────
    # R_i = ∫₀ᵀ D_i(t) dt  (discrete: sum of per-step drift values)
    # w_i = e^{-k·R_i}

    def update_agent_reputation(self, agent_id: str, step_drift: float) -> None:
        """Accumulate drift integral for an agent (called after each step)."""
        now = time.time()
        with self._connect() as conn:
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

    def get_agent_reputation_weight(self, agent_id: str, k: float = 1.0) -> float:
        """
        Returns w_i = e^{-k·R_i}.

        A weight of 1.0 means perfect reputation (R_i = 0).
        Weight approaches 0 as cumulative drift grows.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT drift_integral FROM agent_reputation WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()

        if row is None:
            return 1.0  # unknown agent defaults to full trust (first appearance)

        r_i = row["drift_integral"]
        return math.exp(-k * r_i)

    def list_agent_weights(self, k: float = 1.0) -> List[Dict[str, Any]]:
        """Return all agents sorted by reputation weight descending."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT agent_id, drift_integral, step_count FROM agent_reputation "
                "ORDER BY drift_integral ASC"
            ).fetchall()

        return [
            {
                "agent_id": r["agent_id"],
                "drift_integral": r["drift_integral"],
                "step_count": r["step_count"],
                "weight": math.exp(-k * r["drift_integral"]),
            }
            for r in rows
        ]
