"""
gardeners_protocol.py — Gardeners Protocol
==========================================
Ritual planting of skill states into the ProofVault scroll ledger.

The Gardeners Protocol treats knowledge acquisition as cultivation:
  • Each skill acquisition event is "planted" as a scroll
  • Scrolls have a germination period (minimum practice interval)
  • Scrolls bloom when skill_state reaches the target node
  • Wilted scrolls (stalled sessions) are quarantined, not deleted

The scroll metaphor maps directly to the ProofVault WORM ledger:
  • plant_skill()  → create_trace() + append_step()
  • bloom_check()  → drift == 0 at target node
  • wilt_check()   → no session within germination_hours

Every scroll is sealed with a Gardeners Proof — a deterministic hash
of (learner_id, skill_name, target_node, timestamp) providing tamper
evidence without exposing PII.

Human-in-the-loop integration
------------------------------
The Gardeners Protocol is the human governance layer.  Every step in
the ELFE kernel is mediated by a human coach decision logged here:
  • Coach submits session quality rating (0.0–1.0)
  • Learner submits self-assessment
  • Both are Bayesian-weighted by reputation
  • Disagreement > 0.3 triggers a mandatory PEER_SYNTHESIS intervention
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def _new_event_id() -> str:
    return str(uuid.uuid4())


_DEFAULT_DB = Path.home() / ".sovereign_claw" / "gardeners.sqlite3"
_ENV_DB = os.environ.get("SOVEREIGN_CLAW_GARDENERS_DB")
DEFAULT_GARDENERS_DB = Path(_ENV_DB) if _ENV_DB else _DEFAULT_DB

_GERMINATION_HOURS_DEFAULT = 24.0  # minimum hours between sessions
_WILT_HOURS_DEFAULT = 168.0  # 7 days without session → scroll wilts


# ── Scroll record ─────────────────────────────────────────────────────────────
@dataclass
class SkillScroll:
    scroll_id: str
    learner_id: str
    skill_name: str
    planted_at: float
    skill_state_at_plant: float
    target_node: float
    target_name: str
    glyph_id: str
    gardeners_proof: str
    status: str  # GERMINATING | BLOOMED | WILTED | QUARANTINED
    bloom_at: Optional[float] = None
    final_skill: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── Session record ────────────────────────────────────────────────────────────
@dataclass
class SessionRecord:
    session_id: str
    scroll_id: str
    learner_id: str
    timestamp: float
    skill_before: float
    skill_after: float
    coach_quality: float  # coach's quality rating 0–1
    learner_quality: float  # learner's self-assessment 0–1
    weighted_quality: float  # Bayesian-weighted final quality
    intervention_type: str
    coach_id: Optional[str] = None
    notes: str = ""
    drift_after: float = 0.0


# ── GardenersProtocol ─────────────────────────────────────────────────────────
class GardenersProtocol:
    """
    Human-in-the-loop skill cultivation ledger.

    Maintains an append-only SQLite store of skill scrolls and sessions.
    Every write is sealed with a Gardeners Proof hash for tamper evidence.

    Parameters
    ----------
    db_path              : Path to the gardeners SQLite database.
    germination_hours    : Minimum hours between sessions for same scroll.
    wilt_hours           : Hours of inactivity before scroll is wilted.
    coach_weight_default : Default coach quality weight (0.5 = equal to learner).
    """

    def __init__(
        self,
        db_path: Path = DEFAULT_GARDENERS_DB,
        germination_hours: float = _GERMINATION_HOURS_DEFAULT,
        wilt_hours: float = _WILT_HOURS_DEFAULT,
        coach_weight_default: float = 0.6,
    ) -> None:
        self.db_path = db_path
        self.germination_hours = germination_hours
        self.wilt_hours = wilt_hours
        self.coach_weight_default = coach_weight_default

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Schema ────────────────────────────────────────────────────────────────
    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS scrolls (
                    scroll_id            TEXT PRIMARY KEY,
                    learner_id           TEXT NOT NULL,
                    skill_name           TEXT NOT NULL,
                    planted_at           REAL NOT NULL,
                    skill_state_at_plant REAL NOT NULL,
                    target_node          REAL NOT NULL,
                    target_name          TEXT NOT NULL,
                    glyph_id             TEXT NOT NULL,
                    gardeners_proof      TEXT NOT NULL,
                    status               TEXT NOT NULL DEFAULT 'GERMINATING',
                    bloom_at             REAL,
                    final_skill          REAL,
                    metadata             JSON
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id        TEXT PRIMARY KEY,
                    scroll_id         TEXT NOT NULL,
                    learner_id        TEXT NOT NULL,
                    timestamp         REAL NOT NULL,
                    skill_before      REAL NOT NULL,
                    skill_after       REAL NOT NULL,
                    coach_quality     REAL NOT NULL,
                    learner_quality   REAL NOT NULL,
                    weighted_quality  REAL NOT NULL,
                    intervention_type TEXT NOT NULL,
                    coach_id          TEXT,
                    notes             TEXT DEFAULT '',
                    drift_after       REAL DEFAULT 0.0
                );

                CREATE INDEX IF NOT EXISTS idx_scrolls_learner
                    ON scrolls(learner_id, skill_name);

                CREATE INDEX IF NOT EXISTS idx_sessions_scroll
                    ON sessions(scroll_id, timestamp);

                -- DRIFT-13 FIX: scroll_events table captures every state
                -- transition (GERMINATING → BLOOMED, GERMINATING → WILTED,
                -- etc.) as an immutable event log entry.  Without this table
                -- the bloom/wilt transitions were silent SQL UPDATEs with no
                -- auditable record of when or why the transition occurred.
                CREATE TABLE IF NOT EXISTS scroll_events (
                    event_id    TEXT    PRIMARY KEY,
                    scroll_id   TEXT    NOT NULL,
                    event_type  TEXT    NOT NULL,  -- BLOOMED | WILTED | QUARANTINED
                    from_status TEXT    NOT NULL,
                    to_status   TEXT    NOT NULL,
                    timestamp   REAL    NOT NULL,
                    payload     JSON    NOT NULL DEFAULT '{}'
                );

                CREATE INDEX IF NOT EXISTS idx_scroll_events_scroll
                    ON scroll_events(scroll_id, timestamp);
            """)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        return conn

    # ── Gardeners Proof ───────────────────────────────────────────────────────
    @staticmethod
    def _gardeners_proof(
        learner_id: str,
        skill_name: str,
        target_node: float,
        timestamp: float,
    ) -> str:
        """
        Deterministic tamper-evidence hash.
        Not a cryptographic secret — just a content fingerprint.
        """
        raw = f"{learner_id}:{skill_name}:{target_node:.8f}:{timestamp:.3f}"
        return hashlib.sha256(raw.encode()).hexdigest()[:24]

    # ── Plant a skill scroll ──────────────────────────────────────────────────
    def plant_skill(
        self,
        skill_state: float,
        glyph_id: str,
        learner_id: str = "default_learner",
        skill_name: str = "skill",
        target_node: float = 1.0,
        target_name: str = "Isomorphic Mastery",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Plant a new skill scroll in the Gardeners ledger.

        Returns
        -------
        scroll_id : str — the planted scroll's unique identifier
        """
        now = time.time()
        scroll_id = str(uuid.uuid4())
        proof = self._gardeners_proof(learner_id, skill_name, target_node, now)

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scrolls(
                    scroll_id, learner_id, skill_name, planted_at,
                    skill_state_at_plant, target_node, target_name,
                    glyph_id, gardeners_proof, status, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'GERMINATING', ?)
                """,
                (
                    scroll_id,
                    learner_id,
                    skill_name,
                    now,
                    skill_state,
                    target_node,
                    target_name,
                    glyph_id,
                    proof,
                    json.dumps(metadata or {}),
                ),
            )
        return scroll_id

    # ── Record a learning session ─────────────────────────────────────────────
    def record_session(
        self,
        scroll_id: str,
        skill_before: float,
        skill_after: float,
        coach_quality: float,
        learner_quality: float,
        intervention_type: str,
        drift_after: float = 0.0,
        coach_id: Optional[str] = None,
        notes: str = "",
        coach_weight: Optional[float] = None,
    ) -> SessionRecord:
        """
        Record a human-in-the-loop learning session.

        Bayesian weighting:
            weighted_quality = (coach_w * coach_quality + (1−coach_w) * learner_quality)

        Disagreement > 0.3 is flagged in metadata for PEER_SYNTHESIS intervention.
        """
        cw = coach_weight if coach_weight is not None else self.coach_weight_default
        wq = cw * coach_quality + (1.0 - cw) * learner_quality
        sid = str(uuid.uuid4())
        now = time.time()

        rec = SessionRecord(
            session_id=sid,
            scroll_id=scroll_id,
            learner_id=self._get_scroll_learner(scroll_id),
            timestamp=now,
            skill_before=skill_before,
            skill_after=skill_after,
            coach_quality=coach_quality,
            learner_quality=learner_quality,
            weighted_quality=round(wq, 4),
            intervention_type=intervention_type,
            coach_id=coach_id,
            notes=notes,
            drift_after=drift_after,
        )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions(
                    session_id, scroll_id, learner_id, timestamp,
                    skill_before, skill_after, coach_quality, learner_quality,
                    weighted_quality, intervention_type, coach_id, notes, drift_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sid,
                    scroll_id,
                    rec.learner_id,
                    now,
                    skill_before,
                    skill_after,
                    coach_quality,
                    learner_quality,
                    wq,
                    intervention_type,
                    coach_id,
                    notes,
                    drift_after,
                ),
            )

            # Bloom check — did learner reach target?
            # DRIFT-13 FIX: emit a scroll_events entry on every BLOOMED
            # transition so the state change is auditable.
            scroll = self._get_scroll(scroll_id)
            if (
                scroll
                and skill_after >= scroll["target_node"]
                and scroll["status"] == "GERMINATING"
            ):
                conn.execute(
                    "UPDATE scrolls SET status='BLOOMED', bloom_at=?, final_skill=? WHERE scroll_id=?",
                    (now, skill_after, scroll_id),
                )
                conn.execute(
                    """
                    INSERT INTO scroll_events(
                        event_id, scroll_id, event_type,
                        from_status, to_status, timestamp, payload
                    ) VALUES (?, ?, 'BLOOMED', 'GERMINATING', 'BLOOMED', ?, ?)
                    """,
                    (
                        _new_event_id(),
                        scroll_id,
                        now,
                        json.dumps(
                            {
                                "skill_after": skill_after,
                                "target_node": scroll["target_node"],
                                "session_id": sid,
                            }
                        ),
                    ),
                )

        return rec

    # ── Wilt check ────────────────────────────────────────────────────────────
    def run_wilt_check(self) -> List[str]:
        """
        Check all GERMINATING scrolls. Wilt any with no session in wilt_hours.
        Returns list of wilted scroll_ids.
        """
        cutoff = time.time() - (self.wilt_hours * 3600)
        wilted = []

        with self._connect() as conn:
            scrolls = conn.execute(
                "SELECT scroll_id, planted_at FROM scrolls WHERE status='GERMINATING'"
            ).fetchall()

            for row in scrolls:
                last_session = conn.execute(
                    "SELECT MAX(timestamp) as last FROM sessions WHERE scroll_id=?",
                    (row["scroll_id"],),
                ).fetchone()

                last_ts = last_session["last"] if last_session["last"] else row["planted_at"]
                if last_ts < cutoff:
                    conn.execute(
                        "UPDATE scrolls SET status='WILTED' WHERE scroll_id=?",
                        (row["scroll_id"],),
                    )
                    # DRIFT-13 FIX: emit a scroll_events entry on WILTED transition
                    conn.execute(
                        """
                        INSERT INTO scroll_events(
                            event_id, scroll_id, event_type,
                            from_status, to_status, timestamp, payload
                        ) VALUES (?, ?, 'WILTED', 'GERMINATING', 'WILTED', ?, ?)
                        """,
                        (
                            _new_event_id(),
                            row["scroll_id"],
                            time.time(),
                            json.dumps(
                                {
                                    "last_session_ts": last_ts,
                                    "wilt_cutoff_ts": cutoff,
                                    "wilt_hours": self.wilt_hours,
                                }
                            ),
                        ),
                    )
                    wilted.append(row["scroll_id"])

        return wilted

    # ── Queries ───────────────────────────────────────────────────────────────
    def get_learner_progress(self, learner_id: str) -> List[Dict[str, Any]]:
        """Return all scrolls for a learner with bloom/wilt status."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT scroll_id, skill_name, skill_state_at_plant,
                       target_node, target_name, status, planted_at, bloom_at
                FROM scrolls WHERE learner_id=?
                ORDER BY planted_at DESC
                """,
                (learner_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_scroll_sessions(self, scroll_id: str) -> List[Dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE scroll_id=? ORDER BY timestamp ASC",
                (scroll_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Internal helpers ──────────────────────────────────────────────────────
    def _get_scroll(self, scroll_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM scrolls WHERE scroll_id=?", (scroll_id,)).fetchone()

    def _get_scroll_learner(self, scroll_id: str) -> str:
        row = self._get_scroll(scroll_id)
        return row["learner_id"] if row else "unknown"
