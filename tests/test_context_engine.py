"""Tests for sovereign_claw.context_engine."""

from __future__ import annotations

from sovereign_claw.context_engine import (
    CompactionResult,
    CompactionStrategy,
    ContextEngine,
    ContextMessage,
    ContextSnapshot,
    MessagePriority,
    MessageRole,
    TokenBudget,
)


# ── ContextMessage ───────────────────────────────────────────────────────────


class TestContextMessage:
    def test_creation(self) -> None:
        msg = ContextMessage(role=MessageRole.USER, content="Hello")
        assert msg.role == MessageRole.USER
        assert msg.content == "Hello"
        assert msg.message_id.startswith("msg_")

    def test_token_estimation(self) -> None:
        msg = ContextMessage(role=MessageRole.USER, content="a" * 400)
        # ~4 chars per token
        assert msg.token_count >= 90
        assert msg.token_count <= 110

    def test_to_dict(self) -> None:
        msg = ContextMessage(role=MessageRole.ASSISTANT, content="Hi there")
        d = msg.to_dict()
        assert d["role"] == "assistant"
        assert d["content"] == "Hi there"

    def test_to_full_dict(self) -> None:
        msg = ContextMessage(
            role=MessageRole.SYSTEM,
            content="You are helpful",
            pinned=True,
            priority=MessagePriority.CRITICAL,
        )
        d = msg.to_full_dict()
        assert d["pinned"] is True
        assert d["priority"] == "CRITICAL"

    def test_priority_values(self) -> None:
        assert MessagePriority.CRITICAL.value < MessagePriority.EXPENDABLE.value

    def test_tool_call_id(self) -> None:
        msg = ContextMessage(
            role=MessageRole.TOOL,
            content="result",
            tool_call_id="tc_123",
            name="search",
        )
        d = msg.to_dict()
        assert d["tool_call_id"] == "tc_123"


# ── TokenBudget ──────────────────────────────────────────────────────────────


class TestTokenBudget:
    def test_defaults(self) -> None:
        budget = TokenBudget()
        assert budget.max_context_tokens == 128000
        assert budget.max_output_tokens == 4096

    def test_available_tokens(self) -> None:
        budget = TokenBudget(
            max_context_tokens=10000,
            max_output_tokens=1000,
            reserved_system_tokens=500,
        )
        assert budget.available_tokens == 10000 - 1000 - 500

    def test_compaction_trigger(self) -> None:
        budget = TokenBudget(
            max_context_tokens=10000,
            max_output_tokens=1000,
            reserved_system_tokens=500,
            compaction_threshold=0.8,
        )
        # available = 10000 - 1000 - 500 = 8500
        # trigger = int(8500 * 0.8) = 6800
        assert budget.compaction_trigger == int(8500 * 0.8)

    def test_available_tokens_default(self) -> None:
        budget = TokenBudget()
        # 128000 - 4096 - 2000 = 121904
        assert budget.available_tokens == 128000 - 4096 - 2000


# ── ContextEngine ────────────────────────────────────────────────────────────


class TestContextEngine:
    def test_creation(self) -> None:
        engine = ContextEngine()
        assert len(engine.get_messages()) == 0

    def test_add_system_message(self) -> None:
        engine = ContextEngine()
        engine.add_system("You are a helpful assistant", pinned=True)
        msgs = engine.get_raw_messages()
        assert len(msgs) == 1
        assert msgs[0].role == MessageRole.SYSTEM
        assert msgs[0].pinned is True

    def test_add_user_message(self) -> None:
        engine = ContextEngine()
        engine.add_user("Hello")
        msgs = engine.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "user"

    def test_add_assistant_message(self) -> None:
        engine = ContextEngine()
        engine.add_assistant("Hi there!")
        msgs = engine.get_messages()
        assert msgs[0]["role"] == "assistant"

    def test_add_tool_result(self) -> None:
        engine = ContextEngine()
        engine.add_tool_result("Result data", tool_call_id="tc_123", name="search")
        msgs = engine.get_raw_messages()
        assert msgs[0].role == MessageRole.TOOL
        assert msgs[0].tool_call_id == "tc_123"

    def test_add_message(self) -> None:
        engine = ContextEngine()
        msg = ContextMessage(role=MessageRole.USER, content="Custom")
        engine.add_message(msg)
        assert len(engine.get_messages()) == 1

    def test_clear_messages(self) -> None:
        engine = ContextEngine()
        engine.add_system("System", pinned=True)
        engine.add_user("User")
        engine.clear(keep_system=True)
        msgs = engine.get_raw_messages()
        assert len(msgs) == 1
        assert msgs[0].role == MessageRole.SYSTEM

    def test_clear_all(self) -> None:
        engine = ContextEngine()
        engine.add_system("System")
        engine.add_user("User")
        engine.clear(keep_system=False)
        assert len(engine.get_messages()) == 0

    def test_token_usage(self) -> None:
        engine = ContextEngine()
        engine.add_user("Hello world")
        engine.add_assistant("Hi there")
        usage = engine.token_usage()
        assert usage["used_tokens"] > 0
        assert "by_role" in usage

    def test_stats(self) -> None:
        engine = ContextEngine()
        engine.add_user("msg1")
        engine.add_user("msg2")
        stats = engine.stats()
        assert stats["total_messages_added"] == 2
        assert stats["current_messages"] == 2

    def test_session_id(self) -> None:
        engine = ContextEngine()
        assert engine.session_id.startswith("ctx_")

    def test_message_count(self) -> None:
        engine = ContextEngine()
        assert engine.message_count == 0
        engine.add_user("hello")
        assert engine.message_count == 1

    def test_total_tokens(self) -> None:
        engine = ContextEngine()
        engine.add_user("hello world")
        assert engine.total_tokens > 0

    # ── Compaction ───────────────────────────────────────────────────────

    def test_compact_sliding_window(self) -> None:
        engine = ContextEngine(window_size=3)
        engine.add_system("System", pinned=True)
        for i in range(10):
            engine.add_user(f"Message {i}")
        result = engine.compact(CompactionStrategy.SLIDING_WINDOW)
        assert isinstance(result, CompactionResult)
        # Should have pinned system + last 3 messages
        raw = engine.get_raw_messages()
        assert len(raw) <= 4  # 1 pinned + 3 window

    def test_compact_truncate_oldest(self) -> None:
        budget = TokenBudget(max_context_tokens=500, compaction_threshold=0.5)
        engine = ContextEngine(budget=budget)
        for i in range(20):
            engine.add_user(f"Message number {i} with some extra content")
        result = engine.compact(CompactionStrategy.TRUNCATE_OLDEST)
        assert result.messages_removed > 0

    def test_compact_priority_prune(self) -> None:
        engine = ContextEngine()
        # Add messages with different priorities
        msg_critical = ContextMessage(
            role=MessageRole.USER,
            content="Critical info",
            priority=MessagePriority.CRITICAL,
        )
        msg_expendable = ContextMessage(
            role=MessageRole.USER,
            content="Expendable info",
            priority=MessagePriority.EXPENDABLE,
        )
        engine.add_message(msg_critical)
        engine.add_message(msg_expendable)
        engine.compact(CompactionStrategy.PRIORITY_PRUNE)
        # Critical should remain
        raw = engine.get_raw_messages()
        priorities = [m.priority for m in raw]
        assert MessagePriority.CRITICAL in priorities

    def test_compact_summarize_without_summarizer(self) -> None:
        engine = ContextEngine()
        for i in range(5):
            engine.add_user(f"Message {i}")
        # Without a summarizer, should fall back to sliding window
        result = engine.compact(CompactionStrategy.SUMMARIZE)
        assert isinstance(result, CompactionResult)

    def test_compact_summarize_with_summarizer(self) -> None:
        def mock_summarizer(messages: list[ContextMessage]) -> str:
            return f"Summary of {len(messages)} messages"

        engine = ContextEngine(summarizer=mock_summarizer, window_size=2)
        for i in range(10):
            engine.add_user(f"Message {i}")
        result = engine.compact(CompactionStrategy.SUMMARIZE)
        assert result.messages_removed > 0

    def test_needs_compaction(self) -> None:
        budget = TokenBudget(
            max_context_tokens=200,
            max_output_tokens=50,
            reserved_system_tokens=50,
            compaction_threshold=0.5,
        )
        engine = ContextEngine(budget=budget)
        # available = 100, trigger = 50
        # Add enough messages to exceed trigger
        for i in range(20):
            engine.add_user(f"Message {i} with extra content to use tokens")
        # Auto-compaction should have been triggered
        assert engine.stats()["total_compactions"] > 0

    # ── Snapshots ────────────────────────────────────────────────────────

    def test_snapshot_and_restore(self) -> None:
        engine = ContextEngine()
        engine.add_user("Before snapshot")
        snap = engine.snapshot(label="checkpoint")
        assert isinstance(snap, ContextSnapshot)

        engine.add_user("After snapshot")
        assert len(engine.get_messages()) == 2

        engine.restore_snapshot(snap.snapshot_id)
        assert len(engine.get_messages()) == 1

    def test_restore_nonexistent_snapshot(self) -> None:
        engine = ContextEngine()
        result = engine.restore_snapshot("nonexistent")
        assert result is False

    def test_max_snapshots(self) -> None:
        engine = ContextEngine()
        for i in range(15):
            engine.snapshot(label=f"snap_{i}")
        # Max 10 snapshots
        stats = engine.stats()
        assert stats["snapshots"] <= 10

    # ── Message ordering ─────────────────────────────────────────────────

    def test_message_order_preserved(self) -> None:
        engine = ContextEngine()
        engine.add_system("System prompt")
        engine.add_user("User 1")
        engine.add_assistant("Assistant 1")
        engine.add_user("User 2")
        msgs = engine.get_messages()
        roles = [m["role"] for m in msgs]
        assert roles == ["system", "user", "assistant", "user"]

    # ── Pinned messages survive compaction ────────────────────────────────

    def test_pinned_survive_compaction(self) -> None:
        engine = ContextEngine(window_size=2)
        engine.add_system("I am pinned", pinned=True)
        for i in range(10):
            engine.add_user(f"Msg {i}")
        engine.compact(CompactionStrategy.SLIDING_WINDOW)
        raw = engine.get_raw_messages()
        pinned = [m for m in raw if m.pinned]
        assert len(pinned) >= 1
        assert pinned[0].content == "I am pinned"

    # ── Compaction result details ────────────────────────────────────────

    def test_compaction_result_to_dict(self) -> None:
        engine = ContextEngine(window_size=2)
        for i in range(5):
            engine.add_user(f"Message {i}")
        result = engine.compact(CompactionStrategy.SLIDING_WINDOW)
        d = result.to_dict()
        assert "strategy" in d
        assert d["strategy"] == "sliding_window"
        assert d["messages_removed"] > 0

    def test_clear_returns_count(self) -> None:
        engine = ContextEngine()
        engine.add_user("a")
        engine.add_user("b")
        removed = engine.clear(keep_system=False)
        assert removed == 2
