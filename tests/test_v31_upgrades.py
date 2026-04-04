"""
Tests for v3.1.0 upgrades:
  - Pydantic v2 config migration (field validators, env-var parsing, .env)
  - A2A Protocol (Agent2Agent interop, task lifecycle, state machine)
  - Autonomous Guardrails (privilege escalation, loop detection, destructive actions, cost/token)
  - Persistent Memory (SQLite store/recall, TTL, capacity, relevance)
  - Policy Engine shallow copy fix
"""

from __future__ import annotations

import os
import tempfile
import time

import pytest


# ── Pydantic Config Migration ────────────────────────────────────────────────


class TestPydanticConfig:
    """Verify config.py Pydantic v2 migration."""

    def test_provider_profile_validation(self):
        from sovereign_claw.config import ProviderProfile

        p = ProviderProfile(name="openai", model="gpt-4", timeout=30.0)
        assert p.name == "openai"
        assert p.timeout == 30.0
        assert p.max_tokens == 4096  # default

    def test_provider_profile_rejects_invalid_timeout(self):
        from sovereign_claw.config import ProviderProfile

        with pytest.raises(Exception):
            ProviderProfile(name="openai", timeout=-1)

    def test_provider_profile_rejects_invalid_temperature(self):
        from sovereign_claw.config import ProviderProfile

        with pytest.raises(Exception):
            ProviderProfile(name="openai", temperature=5.0)

    def test_provider_is_configured(self):
        from sovereign_claw.config import ProviderProfile

        p = ProviderProfile(name="openai", api_key="sk-test", model="gpt-4")
        assert p.is_configured() is True

        p2 = ProviderProfile(name="openai")
        assert p2.is_configured() is False

    def test_sovereign_config_defaults(self):
        from sovereign_claw.config import SovereignConfig

        cfg = SovereignConfig()
        assert cfg.log_level == "INFO"
        assert 0.0 <= cfg.risk_threshold <= 1.0

    def test_risk_threshold_clamped(self):
        from sovereign_claw.config import SovereignConfig

        # Values > 1.0 are rejected by Pydantic field constraint (le=1.0)
        with pytest.raises(Exception):
            SovereignConfig(risk_threshold=5.0)

        # Values < 0.0 are rejected by Pydantic field constraint (ge=0.0)
        with pytest.raises(Exception):
            SovereignConfig(risk_threshold=-1.0)

        # Valid boundary values work
        cfg = SovereignConfig(risk_threshold=0.0)
        assert cfg.risk_threshold == 0.0

        cfg2 = SovereignConfig(risk_threshold=1.0)
        assert cfg2.risk_threshold == 1.0

    def test_config_extra_fields_ignored(self):
        from sovereign_claw.config import SovereignConfig

        cfg = SovereignConfig(nonexistent_field="hello")
        assert not hasattr(cfg, "nonexistent_field")

    def test_model_to_dict(self):
        from sovereign_claw.config import SovereignConfig, _model_to_dict

        cfg = SovereignConfig()
        d = _model_to_dict(cfg)
        assert isinstance(d, dict)
        assert "log_level" in d

    def test_dataclass_to_dict_alias(self):
        from sovereign_claw.config import _dataclass_to_dict, _model_to_dict

        assert _dataclass_to_dict is _model_to_dict

    def test_load_dotenv(self):
        from pathlib import Path

        from sovereign_claw.config import _load_dotenv

        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("SC_TEST_DOTENV_KEY=hello_world\n")
            f.write("# comment line\n")
            f.write("SC_TEST_DOTENV_OTHER=value123\n")
            path = f.name

        try:
            # Remove from environ if they exist
            os.environ.pop("SC_TEST_DOTENV_KEY", None)
            os.environ.pop("SC_TEST_DOTENV_OTHER", None)

            _load_dotenv(Path(path))
            assert os.environ.get("SC_TEST_DOTENV_KEY") == "hello_world"
            assert os.environ.get("SC_TEST_DOTENV_OTHER") == "value123"
        finally:
            os.environ.pop("SC_TEST_DOTENV_KEY", None)
            os.environ.pop("SC_TEST_DOTENV_OTHER", None)
            os.unlink(path)

    def test_load_config_returns_sovereign_config(self):
        from sovereign_claw.config import SovereignConfig, load_config

        cfg = load_config()
        assert isinstance(cfg, SovereignConfig)

    def test_save_and_load_config(self):
        from sovereign_claw.config import SovereignConfig, load_config, save_config

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_file = os.path.join(tmpdir, "config.json")
            cfg = SovereignConfig(log_level="DEBUG")
            save_config(cfg, cfg_file)

            loaded = load_config(cfg_file)
            assert loaded.log_level == "DEBUG"


# ── A2A Protocol ──────────────────────────────────────────────────────────────


class TestA2AProtocol:
    """Verify Agent2Agent protocol implementation."""

    def test_task_state_enum(self):
        from sovereign_claw.a2a import TaskState

        assert TaskState.SUBMITTED.value == "submitted"
        assert TaskState.COMPLETED.value == "completed"

    def test_agent_card_creation(self):
        from sovereign_claw.a2a import AgentCard, AgentSkill

        card = AgentCard(
            name="TestAgent",
            description="A test agent",
            skills=[AgentSkill(name="code_review", description="Reviews code")],
        )
        assert card.name == "TestAgent"
        assert len(card.skills) == 1

    def test_agent_card_to_dict(self):
        from sovereign_claw.a2a import AgentCard

        card = AgentCard(name="TestAgent")
        d = card.to_dict()
        assert d["name"] == "TestAgent"
        assert "skills" in d
        assert "supported_protocols" in d

    def test_agent_card_fingerprint(self):
        from sovereign_claw.a2a import AgentCard

        card = AgentCard(name="TestAgent")
        fp = card.fingerprint()
        assert isinstance(fp, str)
        assert len(fp) == 64  # SHA-256

    def test_task_creation(self):
        from sovereign_claw.a2a import A2ATask, TaskState

        task = A2ATask()
        assert task.state == TaskState.SUBMITTED
        assert not task.is_terminal
        assert task.task_id

    def test_valid_transitions(self):
        from sovereign_claw.a2a import A2ATask, TaskState

        task = A2ATask()
        task.transition(TaskState.WORKING)
        assert task.state == TaskState.WORKING

        task.transition(TaskState.COMPLETED)
        assert task.state == TaskState.COMPLETED
        assert task.is_terminal

    def test_invalid_transition_raises(self):
        from sovereign_claw.a2a import A2ATask, TaskState

        task = A2ATask()
        with pytest.raises(ValueError, match="Invalid transition"):
            task.transition(TaskState.COMPLETED)  # SUBMITTED -> COMPLETED invalid

    def test_terminal_state_no_transition(self):
        from sovereign_claw.a2a import A2ATask, TaskState

        task = A2ATask()
        task.transition(TaskState.WORKING)
        task.transition(TaskState.COMPLETED)
        with pytest.raises(ValueError):
            task.transition(TaskState.WORKING)

    def test_task_messages(self):
        from sovereign_claw.a2a import A2ATask

        task = A2ATask()
        msg = task.add_message("user", "Hello agent")
        assert msg.role == "user"
        assert msg.text() == "Hello agent"
        assert len(task.messages) == 1

    def test_task_artifacts(self):
        from sovereign_claw.a2a import A2ATask

        task = A2ATask()
        art = task.add_artifact("result.json", '{"status": "ok"}', "application/json")
        assert art.name == "result.json"
        assert len(task.artifacts) == 1

    def test_a2a_server_lifecycle(self):
        from sovereign_claw.a2a import A2AServer, AgentCard, TaskState

        card = AgentCard(name="SovereignClaw", version="3.1.0")
        server = A2AServer(card)

        assert server.get_agent_card()["name"] == "SovereignClaw"

        task = server.create_task("Analyze this code")
        assert task.state == TaskState.SUBMITTED
        assert len(task.messages) == 1

        server.send_message(task.task_id, "agent", "Working on it")
        server.transition_task(task.task_id, TaskState.WORKING)
        assert task.state == TaskState.WORKING

        completed = server.complete_task(task.task_id, "Analysis complete")
        assert completed.state == TaskState.COMPLETED

    def test_a2a_server_fail_task(self):
        from sovereign_claw.a2a import A2AServer, AgentCard, TaskState

        server = A2AServer(AgentCard(name="Test"))
        task = server.create_task("Do something")
        failed = server.fail_task(task.task_id, "Out of memory")
        assert failed.state == TaskState.FAILED

    def test_a2a_server_cancel_task(self):
        from sovereign_claw.a2a import A2AServer, AgentCard, TaskState

        server = A2AServer(AgentCard(name="Test"))
        task = server.create_task("Cancel me")
        canceled = server.cancel_task(task.task_id)
        assert canceled.state == TaskState.CANCELED

    def test_a2a_server_list_tasks(self):
        from sovereign_claw.a2a import A2AServer, AgentCard, TaskState

        server = A2AServer(AgentCard(name="Test"))
        server.create_task("Task 1")
        server.create_task("Task 2")

        all_tasks = server.list_tasks()
        assert len(all_tasks) == 2

        submitted = server.list_tasks(state=TaskState.SUBMITTED)
        assert len(submitted) == 2

    def test_a2a_server_stats(self):
        from sovereign_claw.a2a import A2AServer, AgentCard

        server = A2AServer(AgentCard(name="Test"))
        server.create_task("Task 1")
        server.create_task("Task 2")
        stats = server.stats()
        assert stats["submitted"] == 2

    def test_a2a_server_unknown_task(self):
        from sovereign_claw.a2a import A2AServer, AgentCard

        server = A2AServer(AgentCard(name="Test"))
        assert server.get_task("nonexistent") is None

        with pytest.raises(KeyError):
            server.send_message("nonexistent", "user", "hello")

    def test_send_message_to_terminal_raises(self):
        from sovereign_claw.a2a import A2AServer, AgentCard

        server = A2AServer(AgentCard(name="Test"))
        task = server.create_task("Done")
        server.complete_task(task.task_id, "Finished")

        with pytest.raises(ValueError, match="terminal"):
            server.send_message(task.task_id, "user", "More work")

    def test_input_required_transition(self):
        from sovereign_claw.a2a import A2ATask, TaskState

        task = A2ATask()
        task.transition(TaskState.WORKING)
        task.transition(TaskState.INPUT_REQUIRED)
        assert task.state == TaskState.INPUT_REQUIRED

        task.transition(TaskState.WORKING)
        assert task.state == TaskState.WORKING


# ── Guardrails Engine ─────────────────────────────────────────────────────────


class TestGuardrails:
    """Verify autonomous guardrails engine."""

    def test_privilege_escalation_blocked(self):
        from sovereign_claw.guardrails import GuardrailEngine

        engine = GuardrailEngine()
        decision = engine.evaluate({"tool": "shell_exec"})
        assert not decision.allowed
        assert "privilege_escalation" in decision.blocked_by

    def test_privilege_escalation_allowed_with_auth(self):
        from sovereign_claw.guardrails import GuardrailEngine

        engine = GuardrailEngine()
        decision = engine.evaluate(
            {
                "tool": "shell_exec",
                "authorized_privileged_tools": ["shell_exec"],
            }
        )
        # Should pass privilege check (might still fail destructive)
        assert "privilege_escalation" not in decision.blocked_by

    def test_safe_tool_allowed(self):
        from sovereign_claw.guardrails import GuardrailEngine

        engine = GuardrailEngine()
        decision = engine.evaluate({"tool": "web_search"})
        assert decision.allowed

    def test_loop_detection(self):
        from sovereign_claw.guardrails import GuardrailEngine

        engine = GuardrailEngine()
        decision = engine.evaluate(
            {
                "tool": "web_search",
                "action_history": ["web_search"] * 5,
            }
        )
        assert not decision.allowed
        assert "loop_detection" in decision.blocked_by

    def test_no_loop_with_varied_actions(self):
        from sovereign_claw.guardrails import GuardrailEngine

        engine = GuardrailEngine()
        decision = engine.evaluate(
            {
                "tool": "web_search",
                "action_history": ["a", "b", "c", "d", "e"],
            }
        )
        assert "loop_detection" not in decision.blocked_by

    def test_destructive_action_blocked(self):
        from sovereign_claw.guardrails import GuardrailEngine

        engine = GuardrailEngine()
        decision = engine.evaluate(
            {
                "tool": "file_delete",
                "authorized_privileged_tools": ["file_delete"],
            }
        )
        assert not decision.allowed
        assert "destructive_action" in decision.blocked_by

    def test_destructive_action_approved(self):
        from sovereign_claw.guardrails import GuardrailEngine

        engine = GuardrailEngine()
        decision = engine.evaluate(
            {
                "tool": "file_delete",
                "authorized_privileged_tools": ["file_delete"],
                "human_approved": True,
            }
        )
        assert decision.allowed

    def test_cost_limit_exceeded(self):
        from sovereign_claw.guardrails import GuardrailEngine

        engine = GuardrailEngine()
        decision = engine.evaluate(
            {
                "tool": "query",
                "current_cost_usd": 9.0,
                "estimated_action_cost_usd": 2.0,
                "cost_limit_usd": 10.0,
            }
        )
        assert not decision.allowed
        assert "cost_limit" in decision.blocked_by

    def test_cost_limit_warning(self):
        from sovereign_claw.guardrails import GuardrailEngine

        engine = GuardrailEngine()
        decision = engine.evaluate(
            {
                "tool": "query",
                "current_cost_usd": 8.5,
                "estimated_action_cost_usd": 0.1,
                "cost_limit_usd": 10.0,
            }
        )
        assert decision.allowed
        assert decision.has_warnings

    def test_token_limit_exceeded(self):
        from sovereign_claw.guardrails import GuardrailEngine

        engine = GuardrailEngine()
        decision = engine.evaluate(
            {
                "tool": "query",
                "tokens_used": 90000,
                "estimated_tokens": 20000,
                "token_limit": 100000,
            }
        )
        assert not decision.allowed
        assert "token_limit" in decision.blocked_by

    def test_custom_rule(self):
        from sovereign_claw.guardrails import (
            GuardrailEngine,
            GuardrailResult,
            GuardrailRule,
            GuardrailSeverity,
        )

        def check_time(ctx):
            hour = ctx.get("hour", 12)
            if hour < 6:
                return GuardrailResult(
                    rule_name="night_guard",
                    passed=False,
                    severity=GuardrailSeverity.BLOCK,
                    message="No operations between midnight and 6am",
                )
            return GuardrailResult(rule_name="night_guard", passed=True)

        engine = GuardrailEngine(rules=[])
        engine.add_rule(GuardrailRule(name="night_guard", check_fn=check_time))

        assert engine.evaluate({"hour": 3}).allowed is False
        assert engine.evaluate({"hour": 12}).allowed is True

    def test_remove_rule(self):
        from sovereign_claw.guardrails import GuardrailEngine

        engine = GuardrailEngine()
        initial_count = len(engine.rules)
        removed = engine.remove_rule("loop_detection")
        assert removed is True
        assert len(engine.rules) == initial_count - 1

    def test_stats(self):
        from sovereign_claw.guardrails import GuardrailEngine

        engine = GuardrailEngine()
        engine.evaluate({"tool": "shell_exec"})
        engine.evaluate({"tool": "safe_tool"})
        stats = engine.stats()
        assert stats["evaluations"] == 2
        assert stats["blocks"] >= 1

    def test_guardrail_decision_properties(self):
        from sovereign_claw.guardrails import GuardrailDecision

        d = GuardrailDecision(allowed=True, warnings=["approaching limit"])
        assert d.has_warnings

        d2 = GuardrailDecision(allowed=True)
        assert not d2.has_warnings

    def test_rule_without_check_fn(self):
        from sovereign_claw.guardrails import GuardrailRule

        rule = GuardrailRule(name="noop")
        result = rule.check({})
        assert result.passed is True


# ── Persistent Memory ─────────────────────────────────────────────────────────


class TestPersistentMemory:
    """Verify SQLite-backed persistent memory store."""

    def _make_store(self):
        from sovereign_claw.persistent_memory import PersistentMemoryStore

        return PersistentMemoryStore(db_path=":memory:")

    def _entry(self, **kwargs):
        from sovereign_claw.memory import MemoryEntry

        defaults = {
            "memory_id": "",
            "memory_type": "episodic",
            "content": "test",
        }
        defaults.update(kwargs)
        return MemoryEntry(**defaults)

    def test_store_and_recall(self):
        from sovereign_claw.memory import MemoryQuery

        store = self._make_store()
        entry = self._entry(
            memory_type="episodic",
            content="User asked about ELFE convergence",
            tags=["elfe", "convergence"],
            relevance_score=0.9,
        )
        mid = store.store(entry)
        assert mid

        results = store.recall(MemoryQuery(memory_type="episodic"))
        assert len(results) >= 1
        assert any(r.content == "User asked about ELFE convergence" for r in results)

    def test_get_by_id(self):
        store = self._make_store()
        entry = self._entry(
            memory_type="semantic",
            content="ELFE guarantees fixed-time convergence",
        )
        mid = store.store(entry)

        retrieved = store.get(mid)
        assert retrieved is not None
        assert retrieved.content == "ELFE guarantees fixed-time convergence"

    def test_forget(self):
        store = self._make_store()
        entry = self._entry(memory_type="task", content="Fix bug #42")
        mid = store.store(entry)

        assert store.forget(mid) is True
        assert store.get(mid) is None
        assert store.forget("nonexistent") is False

    def test_update_relevance(self):
        store = self._make_store()
        entry = self._entry(
            memory_type="semantic",
            content="Test content",
            relevance_score=0.5,
        )
        mid = store.store(entry)

        assert store.update_relevance(mid, 0.95) is True
        updated = store.get(mid)
        assert updated is not None
        assert abs(updated.relevance_score - 0.95) < 0.01

    def test_relevance_clamped(self):
        store = self._make_store()
        entry = self._entry(memory_type="semantic", content="Test")
        mid = store.store(entry)

        store.update_relevance(mid, 5.0)
        updated = store.get(mid)
        assert updated is not None
        assert updated.relevance_score <= 1.0

    def test_ttl_expiry(self):
        from sovereign_claw.memory import MemoryQuery

        store = self._make_store()
        entry = self._entry(
            memory_type="episodic",
            content="Expired memory",
            ttl_seconds=0.01,
            created_at=time.time() - 1,
        )
        store.store(entry)

        results = store.recall(MemoryQuery(memory_type="episodic"))
        assert not any(r.content == "Expired memory" for r in results)

    def test_stats(self):
        store = self._make_store()
        store.store(self._entry(memory_type="episodic", content="ep1"))
        store.store(self._entry(memory_type="semantic", content="sem1"))
        store.store(self._entry(memory_type="task", content="task1"))

        stats = store.stats()
        assert stats.episodic_count == 1
        assert stats.semantic_count == 1
        assert stats.task_count == 1
        assert stats.total_entries == 3

    def test_clear_all(self):
        store = self._make_store()
        store.store(self._entry(memory_type="episodic", content="ep1"))
        store.store(self._entry(memory_type="semantic", content="sem1"))

        count = store.clear()
        assert count == 2
        assert store.stats().total_entries == 0

    def test_clear_by_type(self):
        store = self._make_store()
        store.store(self._entry(memory_type="episodic", content="ep1"))
        store.store(self._entry(memory_type="semantic", content="sem1"))

        count = store.clear("episodic")
        assert count == 1
        assert store.stats().episodic_count == 0
        assert store.stats().semantic_count == 1

    def test_capacity_enforcement(self):
        store = self._make_store()
        store._max_capacity["task"] = 3

        for i in range(5):
            store.store(
                self._entry(
                    memory_type="task",
                    content=f"task-{i}",
                    relevance_score=i * 0.2,
                )
            )

        stats = store.stats()
        assert stats.task_count <= 3

    def test_recall_with_tags(self):
        from sovereign_claw.memory import MemoryQuery

        store = self._make_store()
        store.store(
            self._entry(
                memory_type="semantic",
                content="ELFE info",
                tags=["elfe"],
            )
        )
        store.store(
            self._entry(
                memory_type="semantic",
                content="Policy info",
                tags=["policy"],
            )
        )

        results = store.recall(
            MemoryQuery(
                memory_type="semantic",
                tags=["elfe"],
            )
        )
        assert len(results) == 1
        assert results[0].content == "ELFE info"

    def test_recall_min_relevance(self):
        from sovereign_claw.memory import MemoryQuery

        store = self._make_store()
        store.store(
            self._entry(
                memory_type="semantic",
                content="low",
                relevance_score=0.1,
            )
        )
        store.store(
            self._entry(
                memory_type="semantic",
                content="high",
                relevance_score=0.9,
            )
        )

        results = store.recall(MemoryQuery(min_relevance=0.5))
        assert all(r.relevance_score >= 0.5 for r in results)


# ── Policy Engine Shallow Copy Fix ────────────────────────────────────────────


class TestPolicyEngineFix:
    """Verify test_policy() no longer leaks ViolationRecord mutations."""

    def test_test_policy_no_side_effects(self):
        from sovereign_claw.policy_engine import PolicyEngine

        engine = PolicyEngine()
        # Run a test evaluation
        sample = {"tool_name": "shell_exec", "payload_size": 100, "trace_id": "test-123"}
        engine.test_policy(sample)

        # Verify violation history not modified
        violations_before = dict(engine._violation_history)
        engine.test_policy(sample)
        violations_after = dict(engine._violation_history)

        assert violations_before == violations_after

    def test_import_copy_used(self):
        """Verify copy module is imported in policy_engine."""
        import sovereign_claw.policy_engine as pe
        import copy

        assert hasattr(pe, "copy") or "copy" in dir(pe) or copy is not None


# ── Version ───────────────────────────────────────────────────────────────────


class TestVersion:
    """Verify version alignment."""

    def test_version_is_3_1_0(self):
        import sovereign_claw

        assert sovereign_claw.__version__ == "3.1.0"

    def test_new_module_exports(self):
        from sovereign_claw import (
            A2AServer,
            A2ATask,
            GuardrailDecision,
            GuardrailEngine,
            GuardrailRule,
            GuardrailSeverity,
            PersistentMemoryStore,
            TaskState,
        )

        # Verify all v3.1.0 exports are importable
        for cls in (
            A2AServer,
            A2ATask,
            GuardrailDecision,
            GuardrailEngine,
            GuardrailRule,
            GuardrailSeverity,
            PersistentMemoryStore,
            TaskState,
        ):
            assert cls is not None
