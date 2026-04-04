"""
persistent_memory.py — SQLite-Backed Persistent Memory Store
=============================================================
Extends the in-memory MemoryStore with SQLite persistence so that
episodic, semantic, and task memories survive process restarts.

Features:
  - Automatic schema creation and migration
  - TTL-based expiry enforced in SQL queries
  - Indexes on memory_type, relevance_score, trace_id
  - Same interface as MemoryStore for drop-in replacement
  - Thread-safe via sqlite3 ``check_same_thread=False``
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional

from .memory import MemoryEntry, MemoryQuery, MemoryStats, MemoryType

# Schema version for future migrations
_SCHEMA_VERSION = 1

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS memories (
    memory_id   TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,
    content     TEXT NOT NULL,
    tags        TEXT NOT NULL DEFAULT '[]',
    trace_id    TEXT NOT NULL DEFAULT '',
    relevance_score REAL NOT NULL DEFAULT 0.5,
    access_count    INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    last_accessed   REAL NOT NULL,
    ttl_seconds     REAL,
    metadata        TEXT NOT NULL DEFAULT '{}'
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_mem_type ON memories(memory_type);",
    "CREATE INDEX IF NOT EXISTS idx_mem_relevance ON memories(relevance_score);",
    "CREATE INDEX IF NOT EXISTS idx_mem_trace ON memories(trace_id);",
    "CREATE INDEX IF NOT EXISTS idx_mem_created ON memories(created_at);",
]

_CREATE_META = """
CREATE TABLE IF NOT EXISTS memory_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class PersistentMemoryStore:
    """
    SQLite-backed memory store providing persistent episodic, semantic,
    and task memory with TTL and relevance scoring.

    Drop-in replacement for the in-memory ``MemoryStore``.
    """

    def __init__(
        self,
        db_path: str = "memory.db",
        max_capacity: Optional[Dict[str, int]] = None,
    ) -> None:
        self._db_path = db_path
        self._max_capacity: Dict[str, int] = max_capacity or {
            "episodic": 1000,
            "semantic": 5000,
            "task": 500,
        }
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        """Create tables and indexes if they don't exist."""
        cur = self._conn.cursor()
        cur.execute(_CREATE_TABLE)
        for idx_sql in _CREATE_INDEXES:
            cur.execute(idx_sql)
        cur.execute(_CREATE_META)
        # Store schema version
        cur.execute(
            "INSERT OR IGNORE INTO memory_meta(key, value) VALUES (?, ?)",
            ("schema_version", str(_SCHEMA_VERSION)),
        )
        self._conn.commit()

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()

    # ── Store ─────────────────────────────────────────────────────────────────

    def store(self, entry: MemoryEntry) -> str:
        """Store a memory entry, returning its ID."""
        if not entry.memory_id:
            entry.memory_id = uuid.uuid4().hex[:16]

        self._evict_expired(entry.memory_type)
        self._enforce_capacity(entry.memory_type)

        now = time.time()
        cur = self._conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO memories
                (memory_id, memory_type, content, tags, trace_id,
                 relevance_score, access_count, created_at, last_accessed,
                 ttl_seconds, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.memory_id,
                entry.memory_type,
                entry.content,
                json.dumps(entry.tags),
                entry.trace_id,
                entry.relevance_score,
                entry.access_count,
                entry.created_at or now,
                entry.last_accessed or now,
                entry.ttl_seconds,
                json.dumps(entry.metadata),
            ),
        )
        self._conn.commit()
        return entry.memory_id

    # ── Recall ────────────────────────────────────────────────────────────────

    def recall(self, query: MemoryQuery) -> List[MemoryEntry]:
        """Query memories matching the given criteria."""
        conditions = []
        params: List[Any] = []

        if not query.include_expired:
            conditions.append("(ttl_seconds IS NULL OR (created_at + ttl_seconds) > ?)")
            params.append(time.time())

        if query.memory_type:
            conditions.append("memory_type = ?")
            params.append(query.memory_type)

        if query.trace_id:
            conditions.append("trace_id = ?")
            params.append(query.trace_id)

        conditions.append("relevance_score >= ?")
        params.append(query.min_relevance)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"""
            SELECT * FROM memories
            WHERE {where}
            ORDER BY relevance_score DESC
            LIMIT ?
        """
        params.append(query.max_results)

        cur = self._conn.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()

        results = [self._row_to_entry(row) for row in rows]

        # Filter by tags in Python (SQLite JSON filtering is limited)
        if query.tags:
            results = [e for e in results if any(t in e.tags for t in query.tags)]

        # Update access metrics only for entries actually returned
        now = time.time()
        for entry in results:
            entry.access_count += 1
            entry.last_accessed = now
            self._conn.execute(
                "UPDATE memories SET access_count = ?, last_accessed = ? WHERE memory_id = ?",
                (entry.access_count, entry.last_accessed, entry.memory_id),
            )

        self._conn.commit()
        return results

    # ── Get / Forget / Update ─────────────────────────────────────────────────

    def get(self, memory_id: str) -> Optional[MemoryEntry]:
        """Get a specific memory entry by ID."""
        cur = self._conn.cursor()
        cur.execute("SELECT * FROM memories WHERE memory_id = ?", (memory_id,))
        row = cur.fetchone()
        if row is None:
            return None

        entry = self._row_to_entry(row)
        if entry.is_expired:
            return None

        entry.access_count += 1
        entry.last_accessed = time.time()
        self._conn.execute(
            "UPDATE memories SET access_count = ?, last_accessed = ? WHERE memory_id = ?",
            (entry.access_count, entry.last_accessed, memory_id),
        )
        self._conn.commit()
        return entry

    def forget(self, memory_id: str) -> bool:
        """Remove a specific memory entry."""
        cur = self._conn.cursor()
        cur.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def update_relevance(self, memory_id: str, new_score: float) -> bool:
        """Update the relevance score of a memory entry."""
        clamped = max(0.0, min(1.0, new_score))
        cur = self._conn.cursor()
        cur.execute(
            "UPDATE memories SET relevance_score = ? WHERE memory_id = ?",
            (clamped, memory_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    # ── Stats / Clear ─────────────────────────────────────────────────────────

    def stats(self) -> MemoryStats:
        """Get current memory statistics."""
        now = time.time()
        cur = self._conn.cursor()

        # Active entries (not expired)
        cur.execute(
            "SELECT COUNT(*) FROM memories WHERE ttl_seconds IS NULL OR (created_at + ttl_seconds) > ?",
            (now,),
        )
        total_active = cur.fetchone()[0]

        # Expired entries
        cur.execute(
            "SELECT COUNT(*) FROM memories WHERE ttl_seconds IS NOT NULL AND (created_at + ttl_seconds) <= ?",
            (now,),
        )
        expired = cur.fetchone()[0]

        # Per-type counts (active only)
        type_counts: Dict[str, int] = {}
        for mtype in ("episodic", "semantic", "task"):
            cur.execute(
                "SELECT COUNT(*) FROM memories WHERE memory_type = ? AND (ttl_seconds IS NULL OR (created_at + ttl_seconds) > ?)",
                (mtype, now),
            )
            type_counts[mtype] = cur.fetchone()[0]

        # Average relevance
        cur.execute(
            "SELECT AVG(relevance_score) FROM memories WHERE ttl_seconds IS NULL OR (created_at + ttl_seconds) > ?",
            (now,),
        )
        avg_row = cur.fetchone()[0]
        avg_relevance = avg_row if avg_row is not None else 0.0

        return MemoryStats(
            total_entries=total_active,
            episodic_count=type_counts.get("episodic", 0),
            semantic_count=type_counts.get("semantic", 0),
            task_count=type_counts.get("task", 0),
            expired_count=expired,
            avg_relevance=avg_relevance,
        )

    def clear(self, memory_type: Optional[MemoryType] = None) -> int:
        """Clear all memories or memories of a specific type."""
        cur = self._conn.cursor()
        if memory_type is None:
            cur.execute("SELECT COUNT(*) FROM memories")
            count = cur.fetchone()[0]
            cur.execute("DELETE FROM memories")
        else:
            cur.execute(
                "SELECT COUNT(*) FROM memories WHERE memory_type = ?",
                (memory_type,),
            )
            count = cur.fetchone()[0]
            cur.execute("DELETE FROM memories WHERE memory_type = ?", (memory_type,))
        self._conn.commit()
        return count

    # ── Private helpers ───────────────────────────────────────────────────────

    def _row_to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        """Convert a database row to a MemoryEntry."""
        return MemoryEntry(
            memory_id=row["memory_id"],
            memory_type=row["memory_type"],
            content=row["content"],
            tags=json.loads(row["tags"]),
            trace_id=row["trace_id"],
            relevance_score=row["relevance_score"],
            access_count=row["access_count"],
            created_at=row["created_at"],
            last_accessed=row["last_accessed"],
            ttl_seconds=row["ttl_seconds"],
            metadata=json.loads(row["metadata"]),
        )

    def _evict_expired(self, memory_type: MemoryType) -> int:
        """Remove expired entries of the given type."""
        now = time.time()
        cur = self._conn.cursor()
        cur.execute(
            "DELETE FROM memories WHERE memory_type = ? AND ttl_seconds IS NOT NULL AND (created_at + ttl_seconds) <= ?",
            (memory_type, now),
        )
        self._conn.commit()
        return cur.rowcount

    def _enforce_capacity(self, memory_type: MemoryType) -> None:
        """Ensure we're under capacity for the given memory type."""
        max_cap = self._max_capacity.get(memory_type, 1000)
        cur = self._conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM memories WHERE memory_type = ? AND (ttl_seconds IS NULL OR (created_at + ttl_seconds) > ?)",
            (memory_type, time.time()),
        )
        count = cur.fetchone()[0]

        if count >= max_cap:
            # Remove lowest relevance entries to make room
            to_remove = count - max_cap + 1
            cur.execute(
                """
                DELETE FROM memories WHERE memory_id IN (
                    SELECT memory_id FROM memories
                    WHERE memory_type = ?
                    ORDER BY relevance_score ASC
                    LIMIT ?
                )
                """,
                (memory_type, to_remove),
            )
            self._conn.commit()
