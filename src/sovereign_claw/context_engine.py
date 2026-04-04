"""
context_engine — Conversation Context Management
==================================================
Token-budget-aware context management with session compaction.

Features:
- Conversation message management with role-based tracking
- Token budget awareness with configurable limits per model
- Automatic context compaction (summarization, pruning, sliding window)
- Session continuity across interactions
- Priority-based message retention (system > tool results > recent user > old)
- Context snapshots for branching and rollback
- Governed context: all compaction decisions auditable

The context engine ensures that LLM conversations stay within token
budgets while preserving the most important context for governance.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class MessageRole(str, Enum):
    """Roles in a conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    FUNCTION = "function"


class CompactionStrategy(str, Enum):
    """Strategy for context compaction."""

    SLIDING_WINDOW = "sliding_window"  # Keep last N messages
    SUMMARIZE = "summarize"  # Summarize older messages
    PRIORITY_PRUNE = "priority_prune"  # Drop lowest priority first
    TRUNCATE_OLDEST = "truncate_oldest"  # Remove oldest messages


class MessagePriority(Enum):
    """Priority levels for message retention during compaction."""

    CRITICAL = 0  # System prompts, governance rules — never pruned
    HIGH = 1  # Recent user messages, tool results
    NORMAL = 2  # Regular conversation
    LOW = 3  # Old context, verbose outputs
    EXPENDABLE = 4  # Can be freely dropped


@dataclass
class ContextMessage:
    """A message in the conversation context."""

    role: MessageRole
    content: str
    message_id: str = ""
    name: str = ""
    tool_call_id: str = ""
    timestamp: float = field(default_factory=time.time)
    token_count: int = 0
    priority: MessagePriority = MessagePriority.NORMAL
    metadata: dict[str, Any] = field(default_factory=dict)
    pinned: bool = False  # pinned messages survive compaction

    def __post_init__(self) -> None:
        if not self.message_id:
            self.message_id = f"msg_{uuid.uuid4().hex[:10]}"
        if self.token_count == 0:
            self.token_count = self._estimate_tokens()

    def _estimate_tokens(self) -> int:
        """Rough token estimation (4 chars ≈ 1 token)."""
        return max(1, len(self.content) // 4)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": self.role.value,
            "content": self.content,
        }
        if self.name:
            result["name"] = self.name
        if self.tool_call_id:
            result["tool_call_id"] = self.tool_call_id
        return result

    def to_full_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "role": self.role.value,
            "content": self.content,
            "name": self.name,
            "tool_call_id": self.tool_call_id,
            "timestamp": self.timestamp,
            "token_count": self.token_count,
            "priority": self.priority.name,
            "pinned": self.pinned,
            "metadata": self.metadata,
        }


@dataclass
class TokenBudget:
    """Token budget configuration for a model."""

    max_context_tokens: int = 128000  # Total context window
    max_output_tokens: int = 4096  # Reserved for response
    reserved_system_tokens: int = 2000  # Reserved for system prompt
    compaction_threshold: float = 0.85  # Trigger compaction at 85% usage

    @property
    def available_tokens(self) -> int:
        """Tokens available for conversation (excluding output reserve)."""
        return self.max_context_tokens - self.max_output_tokens - self.reserved_system_tokens

    @property
    def compaction_trigger(self) -> int:
        """Token count that triggers compaction."""
        return int(self.available_tokens * self.compaction_threshold)


@dataclass
class ContextSnapshot:
    """A snapshot of the context state for rollback."""

    snapshot_id: str
    messages: list[ContextMessage]
    total_tokens: int
    timestamp: float
    label: str = ""


@dataclass
class CompactionResult:
    """Result of a context compaction operation."""

    strategy: CompactionStrategy
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    tokens_freed: int
    messages_removed: int
    summary_generated: bool = False
    summary_text: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "messages_before": self.messages_before,
            "messages_after": self.messages_after,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "tokens_freed": self.tokens_freed,
            "messages_removed": self.messages_removed,
            "summary_generated": self.summary_generated,
        }


# Type for summarization function
SummarizationFunc = Callable[[list[ContextMessage]], str]


class ContextEngine:
    """
    Token-budget-aware conversation context manager.

    Usage:
        engine = ContextEngine(budget=TokenBudget(max_context_tokens=128000))

        # Add messages
        engine.add_system("You are a governed AI agent.")
        engine.add_user("Summarize the README")
        engine.add_assistant("The README describes...")

        # Check token usage
        usage = engine.token_usage()
        print(f"Using {usage['used_tokens']}/{usage['available_tokens']} tokens")

        # Get messages for LLM call
        messages = engine.get_messages()

        # Compact if needed
        if engine.needs_compaction():
            result = engine.compact()
    """

    # Maximum snapshots to retain
    MAX_SNAPSHOTS = 10

    def __init__(
        self,
        budget: TokenBudget | None = None,
        default_strategy: CompactionStrategy = CompactionStrategy.SLIDING_WINDOW,
        window_size: int = 50,
        summarizer: SummarizationFunc | None = None,
    ) -> None:
        self._budget = budget or TokenBudget()
        self._default_strategy = default_strategy
        self._window_size = window_size
        self._summarizer = summarizer
        self._messages: list[ContextMessage] = []
        self._snapshots: list[ContextSnapshot] = []
        self._compaction_history: list[CompactionResult] = []
        self._session_id = f"ctx_{uuid.uuid4().hex[:10]}"
        self._total_messages_added = 0
        self._total_compactions = 0

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def message_count(self) -> int:
        return len(self._messages)

    @property
    def total_tokens(self) -> int:
        return sum(m.token_count for m in self._messages)

    def add_message(self, message: ContextMessage) -> ContextMessage:
        """Add a message to the context."""
        self._messages.append(message)
        self._total_messages_added += 1

        # Auto-compact if over threshold
        if self.needs_compaction():
            self.compact()

        return message

    def add_system(self, content: str, pinned: bool = True) -> ContextMessage:
        """Add a system message (pinned by default)."""
        return self.add_message(
            ContextMessage(
                role=MessageRole.SYSTEM,
                content=content,
                priority=MessagePriority.CRITICAL,
                pinned=pinned,
            )
        )

    def add_user(self, content: str) -> ContextMessage:
        """Add a user message."""
        return self.add_message(
            ContextMessage(
                role=MessageRole.USER,
                content=content,
                priority=MessagePriority.HIGH,
            )
        )

    def add_assistant(self, content: str) -> ContextMessage:
        """Add an assistant message."""
        return self.add_message(
            ContextMessage(
                role=MessageRole.ASSISTANT,
                content=content,
                priority=MessagePriority.NORMAL,
            )
        )

    def add_tool_result(
        self,
        content: str,
        tool_call_id: str = "",
        name: str = "",
    ) -> ContextMessage:
        """Add a tool result message."""
        return self.add_message(
            ContextMessage(
                role=MessageRole.TOOL,
                content=content,
                tool_call_id=tool_call_id,
                name=name,
                priority=MessagePriority.HIGH,
            )
        )

    def get_messages(self) -> list[dict[str, Any]]:
        """Get messages formatted for LLM API call."""
        return [m.to_dict() for m in self._messages]

    def get_raw_messages(self) -> list[ContextMessage]:
        """Get raw ContextMessage objects."""
        return list(self._messages)

    def needs_compaction(self) -> bool:
        """Check if context needs compaction based on token budget."""
        return self.total_tokens > self._budget.compaction_trigger

    def compact(
        self,
        strategy: CompactionStrategy | None = None,
    ) -> CompactionResult:
        """
        Compact the context to reduce token usage.

        Args:
            strategy: Compaction strategy to use (or default).

        Returns:
            CompactionResult with details.
        """
        strat = strategy or self._default_strategy
        tokens_before = self.total_tokens
        messages_before = len(self._messages)

        if strat == CompactionStrategy.SLIDING_WINDOW:
            self._compact_sliding_window()
        elif strat == CompactionStrategy.SUMMARIZE:
            summary = self._compact_summarize()
        elif strat == CompactionStrategy.PRIORITY_PRUNE:
            self._compact_priority_prune()
        elif strat == CompactionStrategy.TRUNCATE_OLDEST:
            self._compact_truncate_oldest()

        tokens_after = self.total_tokens
        messages_after = len(self._messages)
        self._total_compactions += 1

        result = CompactionResult(
            strategy=strat,
            messages_before=messages_before,
            messages_after=messages_after,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            tokens_freed=tokens_before - tokens_after,
            messages_removed=messages_before - messages_after,
            summary_generated=strat == CompactionStrategy.SUMMARIZE
            and self._summarizer is not None,
            summary_text=summary if strat == CompactionStrategy.SUMMARIZE else "",
        )
        self._compaction_history.append(result)
        return result

    def snapshot(self, label: str = "") -> ContextSnapshot:
        """Create a snapshot of the current context."""
        snap = ContextSnapshot(
            snapshot_id=f"snap_{uuid.uuid4().hex[:8]}",
            messages=copy.deepcopy(self._messages),
            total_tokens=self.total_tokens,
            timestamp=time.time(),
            label=label,
        )
        self._snapshots.append(snap)
        if len(self._snapshots) > self.MAX_SNAPSHOTS:
            self._snapshots.pop(0)
        return snap

    def restore_snapshot(self, snapshot_id: str) -> bool:
        """Restore context from a snapshot."""
        for snap in self._snapshots:
            if snap.snapshot_id == snapshot_id:
                self._messages = copy.deepcopy(snap.messages)
                return True
        return False

    def clear(self, keep_system: bool = True) -> int:
        """Clear messages. Optionally keep system messages."""
        if keep_system:
            system_msgs = [m for m in self._messages if m.role == MessageRole.SYSTEM]
            removed = len(self._messages) - len(system_msgs)
            self._messages = system_msgs
        else:
            removed = len(self._messages)
            self._messages = []
        return removed

    def token_usage(self) -> dict[str, Any]:
        """Get detailed token usage information."""
        by_role: dict[str, int] = {}
        for m in self._messages:
            by_role[m.role.value] = by_role.get(m.role.value, 0) + m.token_count
        used = self.total_tokens
        available = self._budget.available_tokens
        return {
            "used_tokens": used,
            "available_tokens": available,
            "utilization": used / available if available > 0 else 0.0,
            "needs_compaction": self.needs_compaction(),
            "by_role": by_role,
            "message_count": len(self._messages),
            "budget": {
                "max_context": self._budget.max_context_tokens,
                "max_output": self._budget.max_output_tokens,
                "reserved_system": self._budget.reserved_system_tokens,
                "compaction_threshold": self._budget.compaction_threshold,
            },
        }

    def stats(self) -> dict[str, Any]:
        """Get engine statistics."""
        return {
            "session_id": self._session_id,
            "total_messages_added": self._total_messages_added,
            "total_compactions": self._total_compactions,
            "current_messages": len(self._messages),
            "current_tokens": self.total_tokens,
            "snapshots": len(self._snapshots),
            "compaction_history": [r.to_dict() for r in self._compaction_history[-5:]],
        }

    def _compact_sliding_window(self) -> None:
        """Keep pinned + last N messages."""
        pinned = [m for m in self._messages if m.pinned]
        unpinned = [m for m in self._messages if not m.pinned]
        keep = unpinned[-self._window_size :]
        self._messages = pinned + keep

    def _compact_summarize(self) -> str:
        """Summarize older messages and replace them."""
        summary = ""
        if not self._summarizer:
            # Fallback to sliding window
            self._compact_sliding_window()
            return summary

        pinned = [m for m in self._messages if m.pinned]
        unpinned = [m for m in self._messages if not m.pinned]

        if len(unpinned) <= self._window_size:
            return summary

        old_msgs = unpinned[: -self._window_size]
        recent_msgs = unpinned[-self._window_size :]

        try:
            summary = self._summarizer(old_msgs)
            summary_msg = ContextMessage(
                role=MessageRole.SYSTEM,
                content=f"[Context Summary]\n{summary}",
                priority=MessagePriority.HIGH,
                metadata={"is_summary": True, "summarized_count": len(old_msgs)},
            )
            self._messages = pinned + [summary_msg] + recent_msgs
        except Exception:
            # Fallback to sliding window on summarization failure
            self._compact_sliding_window()

        return summary

    def _compact_priority_prune(self) -> None:
        """Drop lowest priority messages first."""
        target = self._budget.compaction_trigger
        # Sort by priority (highest value = lowest priority) then by timestamp
        candidates = [m for m in self._messages if not m.pinned]
        candidates.sort(key=lambda m: (-m.priority.value, m.timestamp))

        keep_set: set[str] = {m.message_id for m in self._messages if m.pinned}
        current_tokens = sum(m.token_count for m in self._messages if m.pinned)

        for m in reversed(candidates):  # Add back from highest to lowest priority
            if current_tokens + m.token_count <= target:
                keep_set.add(m.message_id)
                current_tokens += m.token_count

        self._messages = [m for m in self._messages if m.message_id in keep_set]

    def _compact_truncate_oldest(self) -> None:
        """Remove oldest non-pinned messages until under threshold."""
        target = self._budget.compaction_trigger
        while self.total_tokens > target:
            # Find oldest non-pinned message
            for i, m in enumerate(self._messages):
                if not m.pinned:
                    self._messages.pop(i)
                    break
            else:
                break  # All pinned, can't reduce further
