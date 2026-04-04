"""
memory.py — Governed State + Memory Layer
==========================================
Provides episodic, semantic, and task memory with governed retention
policies. All memory operations are auditable and TTL-bounded.

Architecture:
  - Episodic Memory:  timestamped event records from execution traces
  - Semantic Memory:  extracted knowledge with relevance scoring
  - Task Memory:      objective-specific context with TTL-based retention
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


MemoryType = Literal["episodic", "semantic", "task"]


@dataclass
class MemoryEntry:
    """A single memory entry with metadata."""

    memory_id: str
    memory_type: MemoryType
    content: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    ttl_seconds: float = 3600.0
    relevance_score: float = 1.0
    access_count: int = 0
    last_accessed: float = 0.0
    trace_id: str = ""
    tags: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.created_at == 0.0:
            self.created_at = time.time()
        if self.last_accessed == 0.0:
            self.last_accessed = self.created_at

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_seconds

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at


@dataclass
class MemoryQuery:
    """Query parameters for memory retrieval."""

    memory_type: Optional[MemoryType] = None
    tags: Optional[List[str]] = None
    min_relevance: float = 0.0
    max_results: int = 10
    include_expired: bool = False
    trace_id: Optional[str] = None


@dataclass
class MemoryStats:
    """Statistics about the memory store."""

    total_entries: int = 0
    episodic_count: int = 0
    semantic_count: int = 0
    task_count: int = 0
    expired_count: int = 0
    avg_relevance: float = 0.0


class MemoryStore:
    """
    Governed memory store with episodic, semantic, and task memory.

    Features:
      - TTL-based retention with automatic expiry
      - Relevance scoring for retrieval prioritization
      - Tag-based organization and querying
      - Configurable capacity limits per memory type
      - Access tracking for usage analytics
    """

    def __init__(
        self,
        max_episodic: int = 1000,
        max_semantic: int = 500,
        max_task: int = 200,
        default_ttl: float = 3600.0,
    ) -> None:
        self._entries: Dict[str, MemoryEntry] = {}
        self._max_capacity: Dict[MemoryType, int] = {
            "episodic": max_episodic,
            "semantic": max_semantic,
            "task": max_task,
        }
        self._default_ttl = default_ttl
        self._id_counter = 0

    def _next_id(self) -> str:
        self._id_counter += 1
        return f"mem_{self._id_counter:06d}"

    def store(
        self,
        content: str,
        memory_type: MemoryType,
        metadata: Optional[Dict[str, Any]] = None,
        ttl_seconds: Optional[float] = None,
        relevance_score: float = 1.0,
        trace_id: str = "",
        tags: Optional[List[str]] = None,
    ) -> MemoryEntry:
        """Store a new memory entry."""
        self._evict_expired(memory_type)
        self._enforce_capacity(memory_type)

        entry = MemoryEntry(
            memory_id=self._next_id(),
            memory_type=memory_type,
            content=content,
            metadata=metadata or {},
            ttl_seconds=ttl_seconds if ttl_seconds is not None else self._default_ttl,
            relevance_score=relevance_score,
            trace_id=trace_id,
            tags=tags or [],
        )
        self._entries[entry.memory_id] = entry
        return entry

    def recall(self, query: MemoryQuery) -> List[MemoryEntry]:
        """Retrieve memories matching the query parameters."""
        results: List[MemoryEntry] = []

        for entry in self._entries.values():
            if not query.include_expired and entry.is_expired:
                continue
            if query.memory_type and entry.memory_type != query.memory_type:
                continue
            if query.trace_id and entry.trace_id != query.trace_id:
                continue
            if entry.relevance_score < query.min_relevance:
                continue
            if query.tags:
                if not any(t in entry.tags for t in query.tags):
                    continue

            entry.access_count += 1
            entry.last_accessed = time.time()
            results.append(entry)

        results.sort(key=lambda e: e.relevance_score, reverse=True)
        return results[: query.max_results]

    def get(self, memory_id: str) -> Optional[MemoryEntry]:
        """Get a specific memory entry by ID."""
        entry = self._entries.get(memory_id)
        if entry and not entry.is_expired:
            entry.access_count += 1
            entry.last_accessed = time.time()
            return entry
        return None

    def forget(self, memory_id: str) -> bool:
        """Remove a specific memory entry."""
        return self._entries.pop(memory_id, None) is not None

    def update_relevance(self, memory_id: str, new_score: float) -> bool:
        """Update the relevance score of a memory entry."""
        entry = self._entries.get(memory_id)
        if entry:
            entry.relevance_score = max(0.0, min(1.0, new_score))
            return True
        return False

    def stats(self) -> MemoryStats:
        """Get current memory statistics."""
        entries = list(self._entries.values())
        active = [e for e in entries if not e.is_expired]
        expired = [e for e in entries if e.is_expired]

        episodic = [e for e in active if e.memory_type == "episodic"]
        semantic = [e for e in active if e.memory_type == "semantic"]
        task = [e for e in active if e.memory_type == "task"]

        total_relevance = sum(e.relevance_score for e in active)
        avg_relevance = total_relevance / len(active) if active else 0.0

        return MemoryStats(
            total_entries=len(active),
            episodic_count=len(episodic),
            semantic_count=len(semantic),
            task_count=len(task),
            expired_count=len(expired),
            avg_relevance=avg_relevance,
        )

    def clear(self, memory_type: Optional[MemoryType] = None) -> int:
        """Clear all memories or memories of a specific type."""
        if memory_type is None:
            count = len(self._entries)
            self._entries.clear()
            return count

        to_remove = [
            mid for mid, entry in self._entries.items() if entry.memory_type == memory_type
        ]
        for mid in to_remove:
            del self._entries[mid]
        return len(to_remove)

    def _evict_expired(self, memory_type: MemoryType) -> int:
        """Remove expired entries of the given type."""
        to_remove = [
            mid
            for mid, entry in self._entries.items()
            if entry.memory_type == memory_type and entry.is_expired
        ]
        for mid in to_remove:
            del self._entries[mid]
        return len(to_remove)

    def _enforce_capacity(self, memory_type: MemoryType) -> None:
        """Ensure we're under capacity for the given memory type."""
        max_cap = self._max_capacity.get(memory_type, 1000)
        type_entries = [
            (mid, entry)
            for mid, entry in self._entries.items()
            if entry.memory_type == memory_type and not entry.is_expired
        ]
        if len(type_entries) >= max_cap:
            # Remove lowest relevance entries
            type_entries.sort(key=lambda x: x[1].relevance_score)
            to_remove = len(type_entries) - max_cap + 1
            for mid, _ in type_entries[:to_remove]:
                del self._entries[mid]
