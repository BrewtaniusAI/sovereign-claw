"""
Tests for v3.0.0 platform modules.
Tests config, model_router, gateway, channels, skills, security,
browser, voice, canvas, sessions, scheduler, mcp_server, and CLI.
"""

from __future__ import annotations

import asyncio
import json

import pytest


# ── Config tests ──────────────────────────────────────────────────────────────
class TestConfig:
    def test_default_config(self):
        from sovereign_claw.config import SovereignConfig

        cfg = SovereignConfig()
        assert cfg.t_max_steps == 16
        assert cfg.risk_threshold == 0.90
        assert cfg.drift_convergence_guarantee is True
        assert cfg.log_level == "INFO"

    def test_provider_profile(self):
        from sovereign_claw.config import ProviderProfile

        p = ProviderProfile(name="openai", api_key="test", model="gpt-4")
        assert p.is_configured()
        p2 = ProviderProfile(name="openai")
        assert not p2.is_configured()

    def test_provider_chain_sorting(self):
        from sovereign_claw.config import ProviderProfile, SovereignConfig

        cfg = SovereignConfig(
            providers=[
                ProviderProfile(name="openai", api_key="k", model="m", priority=2),
                ProviderProfile(name="anthropic", api_key="k", model="m", priority=1),
            ]
        )
        chain = cfg.get_provider_chain()
        assert len(chain) == 2
        assert chain[0].name == "anthropic"

    def test_load_config_defaults(self, tmp_path):
        from sovereign_claw.config import load_config

        cfg = load_config(config_path=str(tmp_path / "nonexistent.json"))
        assert cfg.t_max_steps == 16

    def test_save_and_load(self, tmp_path):
        from sovereign_claw.config import SovereignConfig, load_config, save_config

        cfg = SovereignConfig(t_max_steps=32)
        path = save_config(cfg, str(tmp_path / "cfg.json"))
        assert path.exists()
        loaded = load_config(str(path))
        assert loaded.t_max_steps == 32

    def test_deep_merge(self):
        from sovereign_claw.config import _deep_merge

        base = {"a": 1, "b": {"c": 2, "d": 3}}
        override = {"b": {"c": 99}, "e": 5}
        result = _deep_merge(base, override)
        assert result["b"]["c"] == 99
        assert result["b"]["d"] == 3
        assert result["e"] == 5

    def test_coerce_value(self):
        from sovereign_claw.config import _coerce_value

        assert _coerce_value("true") is True
        assert _coerce_value("false") is False
        assert _coerce_value("42") == 42
        assert _coerce_value("3.14") == 3.14
        assert _coerce_value("hello") == "hello"

    def test_channel_configs(self):
        from sovereign_claw.config import SovereignConfig

        cfg = SovereignConfig()
        assert cfg.discord.enabled is False
        assert cfg.slack.dm_pairing_required is True

    def test_init_config_dir(self, tmp_path, monkeypatch):
        from sovereign_claw import config as cfg_mod

        monkeypatch.setattr(cfg_mod, "DEFAULT_CONFIG_DIR", tmp_path / ".sc")
        monkeypatch.setattr(cfg_mod, "DEFAULT_CONFIG_FILE", tmp_path / ".sc" / "config.json")
        result = cfg_mod.init_config_dir()
        assert result.exists()
        assert (tmp_path / ".sc" / "skills").exists()


# ── Model Router tests ────────────────────────────────────────────────────────
class TestModelRouter:
    def test_no_providers(self):
        from sovereign_claw.model_router import ModelRouter

        router = ModelRouter()
        result = router.call("hello")
        assert not result.success
        assert "No configured providers" in result.error

    def test_circuit_state(self):
        from sovereign_claw.model_router import CircuitState

        cs = CircuitState()
        assert cs.should_attempt()
        for _ in range(3):
            cs.record_failure()
        assert cs.is_open
        assert not cs.should_attempt()
        cs.record_success()
        assert not cs.is_open

    def test_provider_stats(self):
        from sovereign_claw.model_router import ProviderStats

        stats = ProviderStats()
        assert stats.avg_latency_ms == 0.0
        assert stats.success_rate == 1.0
        stats.total_calls = 10
        stats.total_failures = 2
        stats.total_latency_ms = 500.0
        assert stats.avg_latency_ms == 50.0
        assert stats.success_rate == 0.8

    def test_add_provider(self):
        from sovereign_claw.config import ProviderProfile
        from sovereign_claw.model_router import ModelRouter

        router = ModelRouter()
        router.add_provider(ProviderProfile(name="ollama", model="llama3"))
        assert "ollama" in router._providers


# ── Gateway tests ─────────────────────────────────────────────────────────────
class TestGateway:
    def test_create_session(self):
        from sovereign_claw.gateway import Gateway

        gw = Gateway()
        session = gw.create_session(user_id="u1")
        assert session.user_id == "u1"
        assert session.is_alive

    def test_close_session(self):
        from sovereign_claw.gateway import Gateway

        gw = Gateway()
        s = gw.create_session()
        assert gw.close_session(s.session_id)
        assert not s.is_alive

    def test_list_sessions(self):
        from sovereign_claw.gateway import Gateway

        gw = Gateway()
        gw.create_session()
        gw.create_session()
        assert len(gw.list_sessions()) == 2

    def test_handle_heartbeat(self):
        from sovereign_claw.gateway import Gateway, MessageType

        gw = Gateway()
        s = gw.create_session()
        msg = json.dumps({"type": "heartbeat", "payload": {}})
        result = asyncio.get_event_loop().run_until_complete(gw.handle_message(s.session_id, msg))
        assert result is not None
        assert result.type == MessageType.HEARTBEAT_ACK.value

    def test_handle_subscribe(self):
        from sovereign_claw.gateway import Gateway

        gw = Gateway()
        s = gw.create_session()
        msg = json.dumps({"type": "subscribe", "payload": {"topic": "news"}})
        asyncio.get_event_loop().run_until_complete(gw.handle_message(s.session_id, msg))
        assert s.session_id in gw._subscriptions.get("news", set())

    def test_publish(self):
        from sovereign_claw.gateway import Gateway

        gw = Gateway()
        s = gw.create_session()
        msg = json.dumps({"type": "subscribe", "payload": {"topic": "t1"}})
        asyncio.get_event_loop().run_until_complete(gw.handle_message(s.session_id, msg))
        count = gw.publish("t1", {"data": "hello"})
        assert count == 1

    def test_stats(self):
        from sovereign_claw.gateway import Gateway

        gw = Gateway()
        gw.create_session()
        stats = gw.stats()
        assert stats["total_sessions"] == 1
        assert stats["active_sessions"] == 1

    def test_gateway_message_json(self):
        from sovereign_claw.gateway import GatewayMessage

        msg = GatewayMessage(type="test", payload={"k": "v"})
        raw = msg.to_json()
        restored = GatewayMessage.from_json(raw)
        assert restored.type == "test"
        assert restored.payload["k"] == "v"

    def test_invalid_session(self):
        from sovereign_claw.gateway import Gateway

        gw = Gateway()
        result = asyncio.get_event_loop().run_until_complete(gw.handle_message("nonexistent", "{}"))
        assert result is not None
        assert "error" in result.type.lower() or "error" in str(result.payload)


# ── Channels tests ────────────────────────────────────────────────────────────
class TestChannels:
    def test_channel_registry(self):
        from sovereign_claw.channels.connectors import CHANNEL_REGISTRY

        assert "discord" in CHANNEL_REGISTRY
        assert "slack" in CHANNEL_REGISTRY
        assert len(CHANNEL_REGISTRY) == 8

    def test_create_channel(self):
        from sovereign_claw.channels.connectors import create_channel

        ch = create_channel("webchat")
        assert ch.name == "webchat"

    def test_create_unknown_channel(self):
        from sovereign_claw.channels.connectors import create_channel

        with pytest.raises(ValueError, match="Unknown channel"):
            create_channel("nonexistent")

    def test_discord_connect_no_token(self):
        from sovereign_claw.channels.connectors import DiscordChannel

        ch = DiscordChannel()
        result = asyncio.get_event_loop().run_until_complete(ch.connect())
        assert not result

    def test_webchat_connect(self):
        from sovereign_claw.channels.connectors import WebChatChannel

        ch = WebChatChannel()
        result = asyncio.get_event_loop().run_until_complete(ch.connect())
        assert result

    def test_channel_send(self):
        from sovereign_claw.channels.base import ChannelMessage
        from sovereign_claw.channels.connectors import WebChatChannel

        ch = WebChatChannel()
        asyncio.get_event_loop().run_until_complete(ch.connect())
        msg = ChannelMessage(text="hello")
        result = asyncio.get_event_loop().run_until_complete(ch.send(msg))
        assert result
        assert ch.message_count == 1

    def test_channel_message_to_dict(self):
        from sovereign_claw.channels.base import ChannelMessage

        msg = ChannelMessage(text="hello", channel="test")
        d = msg.to_dict()
        assert d["text"] == "hello"
        assert d["channel"] == "test"

    def test_channel_stats(self):
        from sovereign_claw.channels.connectors import WebChatChannel

        ch = WebChatChannel()
        stats = ch.get_stats()
        assert stats["channel"] == "webchat"
        assert stats["messages_processed"] == 0


# ── Skills tests ──────────────────────────────────────────────────────────────
class TestSkills:
    def test_install_bundled(self):
        from sovereign_claw.skills import SkillsManager

        mgr = SkillsManager()
        installed = mgr.install_bundled()
        assert len(installed) >= 6
        assert "web_search" in installed

    def test_evaluate_skill(self):
        from sovereign_claw.skills import SkillsManager

        mgr = SkillsManager()
        mgr.install_bundled()
        result = mgr.evaluate("web_search")
        assert result.passed

    def test_activate_requires_eval(self):
        from sovereign_claw.skills import SkillsManager

        mgr = SkillsManager()
        mgr.install_bundled()
        # Not evaluated yet
        assert not mgr.activate("web_search")
        mgr.evaluate("web_search")
        assert mgr.activate("web_search")

    def test_get_available_tools(self):
        from sovereign_claw.skills import SkillsManager

        mgr = SkillsManager()
        mgr.install_bundled()
        for name in list(mgr._bundled_skills.keys()):
            mgr.evaluate(name)
            mgr.activate(name)
        tools = mgr.get_available_tools()
        assert "web_search" in tools
        assert "execute_python" in tools

    def test_list_skills_filter(self):
        from sovereign_claw.skills import SkillType, SkillsManager

        mgr = SkillsManager()
        mgr.install_bundled()
        bundled = mgr.list_skills(skill_type=SkillType.BUNDLED)
        assert len(bundled) >= 6

    def test_skill_spec_to_dict(self):
        from sovereign_claw.skills import SkillSpec

        spec = SkillSpec(name="test", version="1.0", description="desc")
        d = spec.to_dict()
        assert d["name"] == "test"


# ── Security tests ────────────────────────────────────────────────────────────
class TestSecurity:
    def test_allowlist_deny(self):
        from sovereign_claw.security import AccessDecision, SecurityManager

        sm = SecurityManager(
            allowlist_mode="allowlist",
            allowed_users=["admin"],
            dm_pairing_enabled=False,
        )
        result = sm.check_access("unknown_user")
        assert result.decision == AccessDecision.DENY

    def test_allowlist_allow(self):
        from sovereign_claw.security import AccessDecision, SecurityManager

        sm = SecurityManager(
            allowlist_mode="allowlist",
            allowed_users=["admin"],
            dm_pairing_enabled=False,
        )
        result = sm.check_access("admin")
        assert result.decision == AccessDecision.ALLOW

    def test_denylist(self):
        from sovereign_claw.security import AccessDecision, SecurityManager

        sm = SecurityManager(
            allowlist_mode="open",
            denied_users=["bad_actor"],
            dm_pairing_enabled=False,
        )
        result = sm.check_access("bad_actor")
        assert result.decision == AccessDecision.DENY

    def test_dm_pairing(self):
        from sovereign_claw.security import AccessDecision, SecurityManager

        sm = SecurityManager(
            allowlist_mode="open",
            dm_pairing_enabled=True,
        )
        result = sm.check_access("user1")
        assert result.decision == AccessDecision.CHALLENGE

        pairing = sm.create_pairing("user1")
        assert sm.confirm_pairing("user1", pairing.pairing_code)
        result = sm.check_access("user1")
        assert result.decision == AccessDecision.ALLOW

    def test_secret_detection(self):
        from sovereign_claw.security import SecurityManager

        sm = SecurityManager()
        findings = sm.scan_for_secrets("my key is sk-abc123def456ghijklmnop")
        assert len(findings) > 0

    def test_redact_secrets(self):
        from sovereign_claw.security import SecurityManager

        sm = SecurityManager()
        text = "token: ghp_abcdefghijklmnopqrstuvwxyz1234567890"
        redacted = sm.redact_secrets(text)
        assert "[REDACTED]" in redacted

    def test_reputation(self):
        from sovereign_claw.security import SecurityManager

        sm = SecurityManager()
        assert sm.get_reputation("u1") == 1.0
        sm.update_reputation("u1", -0.3)
        assert sm.get_reputation("u1") == 0.7


# ── Browser tests ─────────────────────────────────────────────────────────────
class TestBrowser:
    def test_connect_disconnect(self):
        from sovereign_claw.browser import BrowserController

        bc = BrowserController()
        result = asyncio.get_event_loop().run_until_complete(bc.connect())
        assert result
        assert bc.is_connected
        asyncio.get_event_loop().run_until_complete(bc.disconnect())
        assert not bc.is_connected

    def test_execute_navigate(self):
        from sovereign_claw.browser import BrowserAction, BrowserActionType, BrowserController

        bc = BrowserController()
        asyncio.get_event_loop().run_until_complete(bc.connect())
        action = BrowserAction(action_type=BrowserActionType.NAVIGATE, target="https://example.com")
        result = asyncio.get_event_loop().run_until_complete(bc.execute(action))
        assert result.success
        assert bc.page_state.url == "https://example.com"

    def test_execute_not_connected(self):
        from sovereign_claw.browser import BrowserAction, BrowserActionType, BrowserController

        bc = BrowserController()
        action = BrowserAction(action_type=BrowserActionType.CLICK, target="#btn")
        result = asyncio.get_event_loop().run_until_complete(bc.execute(action))
        assert not result.success

    def test_stats(self):
        from sovereign_claw.browser import BrowserController

        bc = BrowserController()
        stats = bc.stats()
        assert stats["connected"] is False
        assert stats["actions_executed"] == 0


# ── Voice tests ───────────────────────────────────────────────────────────────
class TestVoice:
    def test_synthesize(self):
        from sovereign_claw.voice import VoiceEngine

        ve = VoiceEngine()
        result = asyncio.get_event_loop().run_until_complete(ve.synthesize("hello"))
        assert result.success

    def test_transcribe(self):
        from sovereign_claw.voice import VoiceEngine

        ve = VoiceEngine()
        result = asyncio.get_event_loop().run_until_complete(ve.transcribe(b"audio"))
        assert result.success

    def test_wake_word(self):
        from sovereign_claw.voice import VoiceEngine

        ve = VoiceEngine(wake_word="sovereign")
        assert ve.detect_wake_word("hey sovereign, do something")
        assert not ve.detect_wake_word("hey siri")

    def test_stats(self):
        from sovereign_claw.voice import VoiceEngine

        ve = VoiceEngine()
        stats = ve.stats()
        assert stats["wake_word"] == "sovereign"


# ── Canvas tests ──────────────────────────────────────────────────────────────
class TestCanvas:
    def test_push_element(self):
        from sovereign_claw.canvas import Canvas, CanvasElement

        c = Canvas()
        elem = CanvasElement(content="Hello")
        assert c.push(elem)
        assert len(c.elements) == 1

    def test_remove_element(self):
        from sovereign_claw.canvas import Canvas, CanvasElement

        c = Canvas()
        elem = CanvasElement(content="bye")
        c.push(elem)
        assert c.remove(elem.element_id)
        assert len(c.elements) == 0

    def test_snapshot_and_restore(self):
        from sovereign_claw.canvas import Canvas, CanvasElement

        c = Canvas()
        c.push(CanvasElement(content="v1"))
        snap = c.snapshot()
        c.clear()
        assert len(c.elements) == 0
        assert c.restore(snap.snapshot_id)
        assert len(c.elements) == 1

    def test_max_elements(self):
        from sovereign_claw.canvas import Canvas, CanvasElement

        c = Canvas(max_elements=2)
        c.push(CanvasElement(content="a"))
        c.push(CanvasElement(content="b"))
        assert not c.push(CanvasElement(content="c"))

    def test_render(self):
        from sovereign_claw.canvas import Canvas, CanvasElement

        c = Canvas()
        c.push(CanvasElement(content="x"))
        rendered = c.render()
        assert rendered["element_count"] == 1


# ── Sessions tests ────────────────────────────────────────────────────────────
class TestSessions:
    def test_create_session(self):
        from sovereign_claw.sessions import SessionManager

        mgr = SessionManager()
        session = mgr.create()
        assert session.is_active

    def test_send_message(self):
        from sovereign_claw.sessions import SessionManager, SessionMessage

        mgr = SessionManager()
        s = mgr.create()
        msg = SessionMessage(sender_id="a1", content="hello")
        assert mgr.send(s.session_id, msg)
        assert len(mgr.history(s.session_id)) == 1

    def test_close_session(self):
        from sovereign_claw.sessions import SessionManager

        mgr = SessionManager()
        s = mgr.create()
        assert mgr.close(s.session_id)
        assert not s.is_active

    def test_ag05_role_isolation(self):
        from sovereign_claw.sessions import AgentRole, AgentSession

        s = AgentSession()
        # AG-05: no agent can plan + execute + validate
        bad_role = AgentRole(
            agent_id="a1",
            role="god",
            can_plan=True,
            can_execute=True,
            can_validate=True,
        )
        assert not s.add_participant(bad_role)
        good_role = AgentRole(
            agent_id="a2",
            role="planner",
            can_plan=True,
            can_execute=False,
            can_validate=False,
        )
        assert s.add_participant(good_role)

    def test_reap_timed_out(self):
        from sovereign_claw.sessions import SessionManager

        mgr = SessionManager()
        s = mgr.create(timeout_s=0.0)
        reaped = mgr.reap_timed_out()
        assert s.session_id in reaped


# ── Scheduler tests ───────────────────────────────────────────────────────────
class TestScheduler:
    def test_create_cron_job(self):
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        job = sched.create_cron_job("test", "0 * * * *", "run hourly")
        assert job.name == "test"
        assert job.cron_expression == "0 * * * *"

    def test_cancel_job(self):
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        job = sched.create_cron_job("test", "0 * * * *", "run")
        assert sched.cancel_job(job.job_id)
        assert not job.enabled

    def test_create_webhook(self):
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        job = sched.create_cron_job("test", "* * * * *", "obj")
        wh = sched.create_webhook("/hook", job.job_id, secret="s3cr3t")
        assert wh.path == "/hook"

    def test_trigger_webhook(self):
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        job = sched.create_cron_job("test", "* * * * *", "obj")
        wh = sched.create_webhook("/hook", job.job_id)
        assert sched.trigger_webhook(wh.endpoint_id, {"data": "test"})

    def test_get_due_jobs(self):
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        sched.create_cron_job("test", "0 * * * *", "obj")
        due = sched.get_due_jobs()
        assert len(due) >= 1

    def test_stats(self):
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        sched.create_cron_job("j1", "0 * * * *", "obj")
        stats = sched.stats()
        assert stats["total_jobs"] == 1


# ── MCP Server tests ─────────────────────────────────────────────────────────
class TestMCPServer:
    def test_add_resource(self):
        from sovereign_claw.mcp_server import MCPResource, MCPServer

        server = MCPServer()
        server.add_resource(MCPResource(uri="file:///test", name="test"))
        assert len(server.list_resources()) == 1

    def test_add_tool(self):
        from sovereign_claw.mcp_server import MCPServer, MCPTool

        server = MCPServer()
        server.add_tool(
            MCPTool(
                name="echo",
                description="Echo text",
                handler=lambda text="": text,
            )
        )
        assert len(server.list_tools()) == 1

    def test_call_tool(self):
        from sovereign_claw.mcp_server import MCPServer, MCPTool

        server = MCPServer()
        server.add_tool(
            MCPTool(
                name="echo",
                description="Echo",
                handler=lambda text="": f"echo: {text}",
            )
        )
        result = asyncio.get_event_loop().run_until_complete(
            server.call_tool("echo", {"text": "hello"})
        )
        assert result.success
        assert "hello" in str(result.content)

    def test_call_unknown_tool(self):
        from sovereign_claw.mcp_server import MCPServer

        server = MCPServer()
        result = asyncio.get_event_loop().run_until_complete(server.call_tool("nonexistent", {}))
        assert not result.success

    def test_handle_initialize(self):
        from sovereign_claw.mcp_server import MCPServer

        server = MCPServer()
        msg = json.dumps({"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": "1"})
        result = asyncio.get_event_loop().run_until_complete(server.handle_message(msg))
        data = json.loads(result)
        assert "result" in data
        assert data["result"]["serverInfo"]["name"] == "sovereign-claw"

    def test_handle_list_tools(self):
        from sovereign_claw.mcp_server import MCPServer, MCPTool

        server = MCPServer()
        server.add_tool(MCPTool(name="t1", description="d1"))
        msg = json.dumps({"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": "2"})
        result = asyncio.get_event_loop().run_until_complete(server.handle_message(msg))
        data = json.loads(result)
        assert len(data["result"]["tools"]) == 1

    def test_handle_invalid_json(self):
        from sovereign_claw.mcp_server import MCPServer

        server = MCPServer()
        result = asyncio.get_event_loop().run_until_complete(server.handle_message("not json"))
        data = json.loads(result)
        assert "error" in data

    def test_jsonrpc_message(self):
        from sovereign_claw.mcp_server import JSONRPCMessage

        msg = JSONRPCMessage(method="test", params={"a": 1}, id="99")
        raw = msg.to_json()
        restored = JSONRPCMessage.from_json(raw)
        assert restored.method == "test"
        assert restored.id == "99"


# ── CLI tests ─────────────────────────────────────────────────────────────────
class TestCLIEnhanced:
    def test_version(self, capsys):
        from sovereign_claw.cli import main

        assert main(["version"]) == 0
        out = capsys.readouterr().out
        assert "3.2.0" in out

    def test_doctor(self, capsys):
        from sovereign_claw.cli import main

        main(["doctor"])
        out = capsys.readouterr().out
        assert "Sovereign Doctor" in out

    def test_gateway(self, capsys):
        from sovereign_claw.cli import main

        assert main(["gateway"]) == 0
        out = capsys.readouterr().out
        assert "Gateway Status" in out

    def test_skills(self, capsys):
        from sovereign_claw.cli import main

        assert main(["skills"]) == 0
        out = capsys.readouterr().out
        assert "Skills" in out

    def test_config(self, capsys):
        from sovereign_claw.cli import main

        assert main(["config"]) == 0
        out = capsys.readouterr().out
        assert "Configuration" in out

    def test_config_json(self, capsys):
        from sovereign_claw.cli import main

        assert main(["config", "--json"]) == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "t_max_steps" in data
