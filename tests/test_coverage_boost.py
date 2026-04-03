"""
Tests to boost coverage for v3.0.0 platform modules.
Targets: web_ui, model_router, channels, scheduler, skills, browser,
sessions, mcp_server, gateway, config, cli.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import MagicMock, patch

import pytest


# ── Web UI tests (0% → ~95%) ─────────────────────────────────────────────────
class TestWebUI:
    def test_dashboard_metrics_defaults(self) -> None:
        from sovereign_claw.web_ui import DashboardMetrics

        m = DashboardMetrics()
        assert m.active_sessions == 0
        assert m.total_messages == 0
        assert m.current_drift == 0.0
        assert m.model_router_calls == 0

    def test_dashboard_metrics_to_dict(self) -> None:
        from sovereign_claw.web_ui import DashboardMetrics

        m = DashboardMetrics(active_sessions=5, total_messages=100, current_drift=0.42)
        d = m.to_dict()
        assert d["active_sessions"] == 5
        assert d["total_messages"] == 100
        assert d["current_drift"] == 0.42
        assert "timestamp" in d

    def test_dashboard_alert_to_dict(self) -> None:
        from sovereign_claw.web_ui import DashboardAlert

        a = DashboardAlert(alert_id="a1", severity="warning", message="high drift", source="elfe")
        d = a.to_dict()
        assert d["alert_id"] == "a1"
        assert d["severity"] == "warning"
        assert d["acknowledged"] is False

    def test_proof_vault_entry_to_dict(self) -> None:
        from sovereign_claw.web_ui import ProofVaultEntry

        e = ProofVaultEntry(step_index=3, trace_id="t1", tool="web_search", status="ok", drift=0.1)
        d = e.to_dict()
        assert d["step_index"] == 3
        assert d["tool"] == "web_search"

    def test_dashboard_state_init(self) -> None:
        from sovereign_claw.web_ui import DashboardState

        ds = DashboardState()
        metrics = ds.get_metrics()
        assert metrics.uptime_seconds >= 0
        assert metrics.active_sessions == 0

    def test_dashboard_state_update_metrics(self) -> None:
        from sovereign_claw.web_ui import DashboardState

        ds = DashboardState()
        m = ds.update_metrics(active_sessions=3, total_messages=50, current_drift=0.2)
        assert m.active_sessions == 3
        assert m.total_messages == 50

    def test_dashboard_state_alerts(self) -> None:
        from sovereign_claw.web_ui import DashboardAlert, DashboardState

        ds = DashboardState()
        ds.add_alert(DashboardAlert(alert_id="a1", severity="error", message="bad"))
        ds.add_alert(DashboardAlert(alert_id="a2", severity="info", message="ok"))
        assert len(ds.get_alerts()) == 2
        assert len(ds.get_alerts(severity="error")) == 1
        assert len(ds.get_alerts(unacknowledged_only=True)) == 2

    def test_dashboard_state_acknowledge_alert(self) -> None:
        from sovereign_claw.web_ui import DashboardAlert, DashboardState

        ds = DashboardState()
        ds.add_alert(DashboardAlert(alert_id="a1", severity="info", message="hi"))
        assert ds.acknowledge_alert("a1")
        assert not ds.acknowledge_alert("nonexistent")
        assert len(ds.get_alerts(unacknowledged_only=True)) == 0

    def test_dashboard_state_alert_max(self) -> None:
        from sovereign_claw.web_ui import DashboardAlert, DashboardState

        ds = DashboardState()
        ds._max_alerts = 3
        for i in range(5):
            ds.add_alert(DashboardAlert(alert_id=f"a{i}", message=f"m{i}"))
        assert len(ds.get_alerts()) == 3

    def test_dashboard_state_vault_entries(self) -> None:
        from sovereign_claw.web_ui import DashboardState, ProofVaultEntry

        ds = DashboardState()
        for i in range(5):
            ds.add_vault_entry(ProofVaultEntry(step_index=i, trace_id=f"t{i}"))
        entries = ds.get_vault_entries(limit=3)
        assert len(entries) == 3

    def test_dashboard_state_vault_max(self) -> None:
        from sovereign_claw.web_ui import DashboardState, ProofVaultEntry

        ds = DashboardState()
        ds._max_vault_cache = 2
        for i in range(5):
            ds.add_vault_entry(ProofVaultEntry(step_index=i))
        assert len(ds.get_vault_entries()) == 2

    def test_dashboard_state_full_state(self) -> None:
        from sovereign_claw.web_ui import DashboardAlert, DashboardState, ProofVaultEntry

        ds = DashboardState()
        ds.update_metrics(active_sessions=1)
        ds.add_alert(DashboardAlert(alert_id="a1", message="test"))
        ds.add_vault_entry(ProofVaultEntry(step_index=0))
        state = ds.get_full_state()
        assert "metrics" in state
        assert "alerts" in state
        assert "vault_entries" in state
        assert len(state["alerts"]) == 1

    def test_get_dashboard_html(self) -> None:
        from sovereign_claw.web_ui import get_dashboard_html

        html = get_dashboard_html()
        assert "Sovereign Claw" in html
        assert "<!DOCTYPE html>" in html

    def test_render_dashboard_json(self) -> None:
        from sovereign_claw.web_ui import DashboardState, render_dashboard_json

        ds = DashboardState()
        ds.update_metrics(active_sessions=2)
        raw = render_dashboard_json(ds)
        data = json.loads(raw)
        assert data["metrics"]["active_sessions"] == 2


# ── Model Router extended tests (50% → ~80%) ─────────────────────────────────
class TestModelRouterExtended:
    def test_router_call_with_mock_provider(self) -> None:
        from sovereign_claw.config import ProviderProfile
        from sovereign_claw.model_router import ModelRouter

        router = ModelRouter()
        profile = ProviderProfile(name="ollama", model="llama3", base_url="http://localhost:11434")
        router.add_provider(profile)

        # Mock the provider's call method to return a response
        router._providers["ollama"].call = MagicMock(return_value="Hello world")  # type: ignore[assignment]

        result = router.call("test prompt")
        assert result.success
        assert result.response == "Hello world"
        assert result.provider_name == "ollama"

    def test_router_call_failover(self) -> None:
        from sovereign_claw.config import ProviderProfile
        from sovereign_claw.model_router import ModelRouter

        router = ModelRouter()
        p1 = ProviderProfile(name="anthropic", api_key="k", model="m", priority=1)
        p2 = ProviderProfile(name="openai", api_key="k", model="m", priority=2)
        router.add_provider(p1)
        router.add_provider(p2)

        # First provider fails, second succeeds
        router._providers["anthropic"].call = MagicMock(side_effect=RuntimeError("down"))  # type: ignore[assignment]
        router._providers["openai"].call = MagicMock(return_value="backup response")  # type: ignore[assignment]

        result = router.call("test", max_retries=1)
        assert result.success
        assert result.provider_name == "openai"

    def test_router_all_providers_fail(self) -> None:
        from sovereign_claw.config import ProviderProfile
        from sovereign_claw.model_router import ModelRouter

        router = ModelRouter()
        p1 = ProviderProfile(name="anthropic", api_key="k", model="m")
        router.add_provider(p1)

        router._providers["anthropic"].call = MagicMock(side_effect=RuntimeError("fail"))  # type: ignore[assignment]

        result = router.call("test", max_retries=1)
        assert not result.success
        assert "fail" in result.error

    def test_router_get_stats(self) -> None:
        from sovereign_claw.config import ProviderProfile
        from sovereign_claw.model_router import ModelRouter

        router = ModelRouter()
        p = ProviderProfile(name="ollama", model="llama3", base_url="http://localhost:11434")
        router.add_provider(p)
        router._providers["ollama"].call = MagicMock(return_value="ok")  # type: ignore[assignment]
        router.call("test")

        stats = router.get_stats()
        assert "ollama" in stats
        assert stats["ollama"]["total_calls"] == 1
        assert stats["ollama"]["success_rate"] == 1.0

    def test_router_call_history(self) -> None:
        from sovereign_claw.config import ProviderProfile
        from sovereign_claw.model_router import ModelRouter

        router = ModelRouter()
        p = ProviderProfile(name="ollama", model="llama3", base_url="http://localhost:11434")
        router.add_provider(p)
        router._providers["ollama"].call = MagicMock(return_value="result")  # type: ignore[assignment]
        router.call("test")

        history = router.call_history
        assert len(history) == 1
        assert history[0].success

    def test_router_retry_with_backoff(self) -> None:
        from sovereign_claw.config import ProviderProfile
        from sovereign_claw.model_router import ModelRouter

        router = ModelRouter()
        p = ProviderProfile(name="ollama", model="llama3", base_url="http://localhost:11434")
        router.add_provider(p)

        call_count = 0

        def flaky_call(prompt: str, **kwargs: object) -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise RuntimeError("transient error")
            return "success after retries"

        router._providers["ollama"].call = flaky_call  # type: ignore[assignment]

        with patch("sovereign_claw.model_router.time.sleep"):
            result = router.call("test", max_retries=3)
        assert result.success
        assert result.response == "success after retries"

    def test_provider_chain_dedup(self) -> None:
        from sovereign_claw.config import ProviderProfile
        from sovereign_claw.model_router import ModelRouter

        router = ModelRouter(
            profiles=[
                ProviderProfile(name="ollama", model="m", base_url="http://localhost:11434"),
                ProviderProfile(name="ollama", model="m2", base_url="http://localhost:11434"),
            ]
        )
        chain = router._get_provider_chain()
        assert len(chain) == 1  # deduplicated


# ── Channel connector tests ──────────────────────────────────────────────────
class TestChannelConnectors:
    def test_discord_connect_with_token(self) -> None:
        from sovereign_claw.channels.connectors import DiscordChannel

        ch = DiscordChannel(token="test-token")
        result = asyncio.get_event_loop().run_until_complete(ch.connect())
        assert result
        assert ch.status.value == "connected"

    def test_discord_send_connected(self) -> None:
        from sovereign_claw.channels.base import ChannelMessage
        from sovereign_claw.channels.connectors import DiscordChannel

        ch = DiscordChannel(token="test-token")
        asyncio.get_event_loop().run_until_complete(ch.connect())
        msg = ChannelMessage(text="hello discord")
        result = asyncio.get_event_loop().run_until_complete(ch.send(msg))
        assert result

    def test_discord_send_disconnected(self) -> None:
        from sovereign_claw.channels.base import ChannelMessage
        from sovereign_claw.channels.connectors import DiscordChannel

        ch = DiscordChannel(token="test-token")
        msg = ChannelMessage(text="hello")
        result = asyncio.get_event_loop().run_until_complete(ch.send(msg))
        assert not result

    def test_discord_disconnect(self) -> None:
        from sovereign_claw.channels.connectors import DiscordChannel

        ch = DiscordChannel(token="test-token")
        asyncio.get_event_loop().run_until_complete(ch.connect())
        asyncio.get_event_loop().run_until_complete(ch.disconnect())
        assert ch.status.value == "disconnected"

    def test_slack_connect_send(self) -> None:
        from sovereign_claw.channels.base import ChannelMessage
        from sovereign_claw.channels.connectors import SlackChannel

        ch = SlackChannel(token="xoxb-test")
        assert asyncio.get_event_loop().run_until_complete(ch.connect())
        msg = ChannelMessage(text="hello slack")
        assert asyncio.get_event_loop().run_until_complete(ch.send(msg))
        asyncio.get_event_loop().run_until_complete(ch.disconnect())

    def test_slack_connect_no_token(self) -> None:
        from sovereign_claw.channels.connectors import SlackChannel

        ch = SlackChannel()
        assert not asyncio.get_event_loop().run_until_complete(ch.connect())

    def test_telegram_connect_send(self) -> None:
        from sovereign_claw.channels.base import ChannelMessage
        from sovereign_claw.channels.connectors import TelegramChannel

        ch = TelegramChannel(token="bot-token")
        assert asyncio.get_event_loop().run_until_complete(ch.connect())
        msg = ChannelMessage(text="hello telegram")
        assert asyncio.get_event_loop().run_until_complete(ch.send(msg))
        asyncio.get_event_loop().run_until_complete(ch.disconnect())

    def test_telegram_no_token(self) -> None:
        from sovereign_claw.channels.connectors import TelegramChannel

        ch = TelegramChannel()
        assert not asyncio.get_event_loop().run_until_complete(ch.connect())

    def test_whatsapp_connect_send(self) -> None:
        from sovereign_claw.channels.base import ChannelMessage
        from sovereign_claw.channels.connectors import WhatsAppChannel

        ch = WhatsAppChannel(access_token="wa-token", phone_number_id="123")
        assert asyncio.get_event_loop().run_until_complete(ch.connect())
        msg = ChannelMessage(text="hello whatsapp")
        assert asyncio.get_event_loop().run_until_complete(ch.send(msg))

    def test_whatsapp_no_token(self) -> None:
        from sovereign_claw.channels.connectors import WhatsAppChannel

        ch = WhatsAppChannel()
        assert not asyncio.get_event_loop().run_until_complete(ch.connect())

    def test_irc_connect_send(self) -> None:
        from sovereign_claw.channels.base import ChannelMessage
        from sovereign_claw.channels.connectors import IRCChannel

        ch = IRCChannel(server="irc.example.com")
        assert asyncio.get_event_loop().run_until_complete(ch.connect())
        msg = ChannelMessage(text="hello irc")
        assert asyncio.get_event_loop().run_until_complete(ch.send(msg))
        asyncio.get_event_loop().run_until_complete(ch.disconnect())

    def test_irc_no_server(self) -> None:
        from sovereign_claw.channels.connectors import IRCChannel

        ch = IRCChannel()
        assert not asyncio.get_event_loop().run_until_complete(ch.connect())

    def test_matrix_connect_send(self) -> None:
        from sovereign_claw.channels.base import ChannelMessage
        from sovereign_claw.channels.connectors import MatrixChannel

        ch = MatrixChannel(homeserver="https://matrix.org", access_token="mx-token")
        assert asyncio.get_event_loop().run_until_complete(ch.connect())
        msg = ChannelMessage(text="hello matrix")
        assert asyncio.get_event_loop().run_until_complete(ch.send(msg))
        asyncio.get_event_loop().run_until_complete(ch.disconnect())

    def test_matrix_no_config(self) -> None:
        from sovereign_claw.channels.connectors import MatrixChannel

        ch = MatrixChannel()
        assert not asyncio.get_event_loop().run_until_complete(ch.connect())

    def test_signal_connect_send(self) -> None:
        from sovereign_claw.channels.base import ChannelMessage
        from sovereign_claw.channels.connectors import SignalChannel

        ch = SignalChannel(phone_number="+1234567890")
        assert asyncio.get_event_loop().run_until_complete(ch.connect())
        msg = ChannelMessage(text="hello signal")
        assert asyncio.get_event_loop().run_until_complete(ch.send(msg))
        asyncio.get_event_loop().run_until_complete(ch.disconnect())

    def test_signal_no_phone(self) -> None:
        from sovereign_claw.channels.connectors import SignalChannel

        ch = SignalChannel()
        assert not asyncio.get_event_loop().run_until_complete(ch.connect())

    def test_webchat_disconnect(self) -> None:
        from sovereign_claw.channels.connectors import WebChatChannel

        ch = WebChatChannel()
        asyncio.get_event_loop().run_until_complete(ch.connect())
        asyncio.get_event_loop().run_until_complete(ch.disconnect())
        assert ch.status.value == "disconnected"

    def test_channel_on_message_dispatch(self) -> None:
        from sovereign_claw.channels.base import ChannelMessage
        from sovereign_claw.channels.connectors import WebChatChannel

        ch = WebChatChannel()
        asyncio.get_event_loop().run_until_complete(ch.connect())

        received: list[ChannelMessage] = []

        class Handler:
            async def handle(self, message: ChannelMessage) -> None:
                received.append(message)

        ch.on_message(Handler())  # type: ignore[arg-type]
        msg = ChannelMessage(text="dispatched")
        asyncio.get_event_loop().run_until_complete(ch._dispatch(msg))
        assert len(received) == 1
        assert received[0].text == "dispatched"


# ── Scheduler extended tests ─────────────────────────────────────────────────
class TestSchedulerExtended:
    def test_interval_job(self) -> None:
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        job = sched.create_interval_job("poll", 60.0, "poll data")
        assert job.interval_seconds == 60.0
        assert job.next_run_at > 0

    def test_once_job(self) -> None:
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        job = sched.create_once_job("deploy", time.time() - 1, "deploy app")
        assert job.is_due

    def test_once_job_completed_not_due(self) -> None:
        from sovereign_claw.scheduler import JobStatus, Scheduler

        sched = Scheduler()
        job = sched.create_once_job("deploy", time.time() - 1, "deploy app")
        job.status = JobStatus.COMPLETED
        assert not job.is_due

    def test_pause_resume_job(self) -> None:
        from sovereign_claw.scheduler import JobStatus, Scheduler

        sched = Scheduler()
        job = sched.create_cron_job("test", "* * * * *", "run")
        assert sched.pause_job(job.job_id)
        assert job.status == JobStatus.PAUSED
        assert not job.is_due
        assert sched.resume_job(job.job_id)
        assert job.status == JobStatus.PENDING

    def test_pause_nonexistent(self) -> None:
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        assert not sched.pause_job("nonexistent")
        assert not sched.resume_job("nonexistent")

    def test_cancel_nonexistent(self) -> None:
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        assert not sched.cancel_job("nonexistent")

    def test_get_job(self) -> None:
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        job = sched.create_cron_job("test", "* * * * *", "run")
        assert sched.get_job(job.job_id) is not None
        assert sched.get_job("nonexistent") is None

    def test_list_jobs(self) -> None:
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        sched.create_cron_job("j1", "* * * * *", "run")
        j2 = sched.create_cron_job("j2", "* * * * *", "run")
        sched.cancel_job(j2.job_id)
        assert len(sched.list_jobs(enabled_only=True)) == 1
        assert len(sched.list_jobs(enabled_only=False)) == 2

    def test_list_webhooks(self) -> None:
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        job = sched.create_cron_job("test", "* * * * *", "run")
        sched.create_webhook("/h1", job.job_id)
        sched.create_webhook("/h2", job.job_id)
        assert len(sched.list_webhooks()) == 2

    def test_trigger_webhook_nonexistent(self) -> None:
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        assert not sched.trigger_webhook("nonexistent", {})

    def test_job_to_dict(self) -> None:
        from sovereign_claw.scheduler import Scheduler

        sched = Scheduler()
        job = sched.create_cron_job("test", "0 * * * *", "run hourly")
        d = job.to_dict()
        assert d["name"] == "test"
        assert d["schedule_type"] == "cron"

    def test_failed_job_retry_limit(self) -> None:
        from sovereign_claw.scheduler import JobStatus, ScheduleType, ScheduledJob

        job = ScheduledJob(
            name="retry-test",
            schedule_type=ScheduleType.CRON,
            status=JobStatus.FAILED,
            retry_count=3,
            max_retries=3,
        )
        assert not job.is_due

    def test_running_job_not_due(self) -> None:
        from sovereign_claw.scheduler import JobStatus, ScheduledJob

        job = ScheduledJob(name="running", status=JobStatus.RUNNING)
        assert not job.is_due

    def test_disabled_job_not_due(self) -> None:
        from sovereign_claw.scheduler import ScheduledJob

        job = ScheduledJob(name="disabled", enabled=False)
        assert not job.is_due


# ── Skills extended tests ────────────────────────────────────────────────────
class TestSkillsExtended:
    def test_deactivate_skill(self) -> None:
        from sovereign_claw.skills import SkillsManager

        mgr = SkillsManager()
        mgr.install_bundled()
        mgr.evaluate("web_search")
        mgr.activate("web_search")
        assert mgr.deactivate("web_search")
        skill = mgr.get_skill("web_search")
        assert skill is not None
        assert not skill.is_active

    def test_deactivate_nonexistent(self) -> None:
        from sovereign_claw.skills import SkillsManager

        mgr = SkillsManager()
        assert not mgr.deactivate("nonexistent")

    def test_get_skill(self) -> None:
        from sovereign_claw.skills import SkillsManager

        mgr = SkillsManager()
        mgr.install_bundled()
        assert mgr.get_skill("web_search") is not None
        assert mgr.get_skill("nonexistent") is None

    def test_list_active_only(self) -> None:
        from sovereign_claw.skills import SkillsManager

        mgr = SkillsManager()
        mgr.install_bundled()
        mgr.evaluate("web_search")
        mgr.activate("web_search")
        active = mgr.list_skills(active_only=True)
        assert len(active) == 1

    def test_evaluate_nonexistent(self) -> None:
        from sovereign_claw.skills import SkillsManager

        mgr = SkillsManager()
        result = mgr.evaluate("ghost")
        assert not result.passed

    def test_evaluate_deprecated_skill(self) -> None:
        from sovereign_claw.skills import SkillSpec, SkillsManager

        mgr = SkillsManager()
        spec = SkillSpec(name="old_skill", version="0.1", description="deprecated", deprecated=True)
        mgr.install(spec)
        result = mgr.evaluate("old_skill")
        assert not result.passed
        assert any("deprecated" in e for e in result.errors)

    def test_discover_workspace_skills(self, tmp_path: object) -> None:
        from sovereign_claw.skills import SkillsManager

        import pathlib

        ws = pathlib.Path(str(tmp_path)) / "workspace"
        ws.mkdir()
        skill_dir = ws / "my_skill"
        skill_dir.mkdir()
        manifest = skill_dir / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "name": "my_skill",
                    "version": "1.0.0",
                    "description": "A workspace skill",
                    "tools_provided": ["my_tool"],
                }
            )
        )

        mgr = SkillsManager()
        discovered = mgr.discover_workspace_skills(str(ws))
        assert len(discovered) == 1
        assert discovered[0].name == "my_skill"

    def test_discover_workspace_no_dir(self) -> None:
        from sovereign_claw.skills import SkillsManager

        mgr = SkillsManager()
        assert mgr.discover_workspace_skills("/nonexistent/path") == []

    def test_discover_workspace_bad_manifest(self, tmp_path: object) -> None:
        import pathlib

        from sovereign_claw.skills import SkillsManager

        ws = pathlib.Path(str(tmp_path)) / "workspace"
        ws.mkdir()
        skill_dir = ws / "bad_skill"
        skill_dir.mkdir()
        (skill_dir / "manifest.json").write_text("not valid json{{{")

        mgr = SkillsManager()
        discovered = mgr.discover_workspace_skills(str(ws))
        assert len(discovered) == 0

    def test_skill_record_use(self) -> None:
        from sovereign_claw.skills import SkillsManager

        mgr = SkillsManager()
        mgr.install_bundled()
        skill = mgr.get_skill("web_search")
        assert skill is not None
        skill.record_use()
        assert skill.use_count == 1
        assert skill.last_used_at > 0

    def test_activate_nonexistent(self) -> None:
        from sovereign_claw.skills import SkillsManager

        mgr = SkillsManager()
        assert not mgr.activate("nonexistent")


# ── Browser extended tests ───────────────────────────────────────────────────
class TestBrowserExtended:
    def test_all_action_types(self) -> None:
        from sovereign_claw.browser import BrowserAction, BrowserActionType, BrowserController

        bc = BrowserController()
        asyncio.get_event_loop().run_until_complete(bc.connect())

        for action_type in [
            BrowserActionType.CLICK,
            BrowserActionType.TYPE,
            BrowserActionType.SCREENSHOT,
            BrowserActionType.EVALUATE,
            BrowserActionType.WAIT,
            BrowserActionType.SCROLL,
            BrowserActionType.SELECT,
            BrowserActionType.EXTRACT,
            BrowserActionType.PDF,
        ]:
            action = BrowserAction(action_type=action_type, target="#el", value="test")
            result = asyncio.get_event_loop().run_until_complete(bc.execute(action))
            assert result.success, f"Action {action_type} failed"
            assert result.duration_ms >= 0

    def test_action_history(self) -> None:
        from sovereign_claw.browser import BrowserAction, BrowserActionType, BrowserController

        bc = BrowserController()
        asyncio.get_event_loop().run_until_complete(bc.connect())
        action = BrowserAction(action_type=BrowserActionType.NAVIGATE, target="https://test.com")
        asyncio.get_event_loop().run_until_complete(bc.execute(action))
        assert len(bc.action_history) == 1

    def test_page_state_after_navigate(self) -> None:
        from sovereign_claw.browser import BrowserAction, BrowserActionType, BrowserController

        bc = BrowserController()
        asyncio.get_event_loop().run_until_complete(bc.connect())
        action = BrowserAction(action_type=BrowserActionType.NAVIGATE, target="https://gov.ai")
        asyncio.get_event_loop().run_until_complete(bc.execute(action))
        assert bc.page_state.url == "https://gov.ai"

    def test_custom_viewport(self) -> None:
        from sovereign_claw.browser import BrowserController

        bc = BrowserController(viewport_width=1920, viewport_height=1080)
        assert bc.viewport_width == 1920
        assert bc.viewport_height == 1080


# ── Sessions extended tests ──────────────────────────────────────────────────
class TestSessionsExtended:
    def test_create_with_valid_participants(self) -> None:
        from sovereign_claw.sessions import AgentRole, SessionManager

        mgr = SessionManager()
        roles = [
            AgentRole(agent_id="a1", role="planner", can_plan=True),
            AgentRole(agent_id="a2", role="executor", can_execute=True),
        ]
        session = mgr.create(participants=roles)
        assert session.is_active
        assert len(session.participants) == 2

    def test_create_with_ag05_violation(self) -> None:
        from sovereign_claw.sessions import AgentRole, SessionManager

        mgr = SessionManager()
        roles = [
            AgentRole(
                agent_id="god_agent",
                role="god",
                can_plan=True,
                can_execute=True,
                can_validate=True,
            )
        ]
        with pytest.raises(ValueError, match="AG-05"):
            mgr.create(participants=roles)

    def test_session_message_to_dict(self) -> None:
        from sovereign_claw.sessions import SessionMessage

        msg = SessionMessage(sender_id="a1", content="hello", session_id="s1")
        d = msg.to_dict()
        assert d["sender_id"] == "a1"
        assert d["content"] == "hello"

    def test_send_to_inactive_session(self) -> None:
        from sovereign_claw.sessions import SessionManager, SessionMessage

        mgr = SessionManager()
        s = mgr.create()
        mgr.close(s.session_id)
        msg = SessionMessage(sender_id="a1", content="too late")
        assert not mgr.send(s.session_id, msg)

    def test_send_nonexistent_session(self) -> None:
        from sovereign_claw.sessions import SessionManager, SessionMessage

        mgr = SessionManager()
        msg = SessionMessage(sender_id="a1", content="nowhere")
        assert not mgr.send("nonexistent", msg)

    def test_history_nonexistent(self) -> None:
        from sovereign_claw.sessions import SessionManager

        mgr = SessionManager()
        assert mgr.history("nonexistent") == []

    def test_close_nonexistent(self) -> None:
        from sovereign_claw.sessions import SessionManager

        mgr = SessionManager()
        assert not mgr.close("nonexistent")

    def test_list_sessions_all(self) -> None:
        from sovereign_claw.sessions import SessionManager

        mgr = SessionManager()
        mgr.create()
        s2 = mgr.create()
        mgr.close(s2.session_id)
        active = mgr.list_sessions(active_only=True)
        all_sessions = mgr.list_sessions(active_only=False)
        assert len(active) == 1
        assert len(all_sessions) == 2

    def test_stats(self) -> None:
        from sovereign_claw.sessions import SessionManager, SessionMessage

        mgr = SessionManager()
        s = mgr.create()
        mgr.send(s.session_id, SessionMessage(sender_id="a1", content="hi"))
        stats = mgr.stats()
        assert stats["total_sessions"] == 1
        assert stats["active_sessions"] == 1
        assert stats["total_messages"] == 1


# ── MCP Server extended tests ────────────────────────────────────────────────
class TestMCPServerExtended:
    def test_async_handler(self) -> None:
        from sovereign_claw.mcp_server import MCPServer, MCPTool

        async def async_echo(text: str = "") -> str:
            return f"async: {text}"

        server = MCPServer()
        server.add_tool(MCPTool(name="async_echo", description="Async echo", handler=async_echo))
        result = asyncio.get_event_loop().run_until_complete(
            server.call_tool("async_echo", {"text": "hello"})
        )
        assert result.success
        assert "async: hello" in str(result.content)

    def test_tool_handler_error(self) -> None:
        from sovereign_claw.mcp_server import MCPServer, MCPTool

        def broken_handler() -> str:
            raise RuntimeError("tool broke")

        server = MCPServer()
        server.add_tool(MCPTool(name="broken", description="Broken", handler=broken_handler))
        result = asyncio.get_event_loop().run_until_complete(server.call_tool("broken", {}))
        assert not result.success
        assert "tool broke" in result.error

    def test_tool_no_handler(self) -> None:
        from sovereign_claw.mcp_server import MCPServer, MCPTool

        server = MCPServer()
        server.add_tool(MCPTool(name="empty", description="No handler"))
        result = asyncio.get_event_loop().run_until_complete(server.call_tool("empty", {}))
        assert not result.success
        assert "no handler" in result.error

    def test_add_get_prompt(self) -> None:
        from sovereign_claw.mcp_server import MCPPrompt, MCPServer

        server = MCPServer()
        server.add_prompt(MCPPrompt(name="greet", description="Greeting", template="Hello {name}"))
        p = server.get_prompt("greet")
        assert p is not None
        assert p.template == "Hello {name}"
        assert server.get_prompt("nonexistent") is None

    def test_list_prompts(self) -> None:
        from sovereign_claw.mcp_server import MCPPrompt, MCPServer

        server = MCPServer()
        server.add_prompt(MCPPrompt(name="p1", description="d1"))
        assert len(server.list_prompts()) == 1

    def test_get_resource(self) -> None:
        from sovereign_claw.mcp_server import MCPResource, MCPServer

        server = MCPServer()
        server.add_resource(MCPResource(uri="file:///test", name="test", content="data"))
        r = server.get_resource("file:///test")
        assert r is not None
        assert r.content == "data"
        assert server.get_resource("nonexistent") is None

    def test_get_tool(self) -> None:
        from sovereign_claw.mcp_server import MCPServer, MCPTool

        server = MCPServer()
        server.add_tool(MCPTool(name="t1", description="d1"))
        assert server.get_tool("t1") is not None
        assert server.get_tool("nonexistent") is None

    def test_handle_resources_list(self) -> None:
        from sovereign_claw.mcp_server import MCPResource, MCPServer

        server = MCPServer()
        server.add_resource(MCPResource(uri="file:///a", name="a"))
        msg = json.dumps({"jsonrpc": "2.0", "method": "resources/list", "params": {}, "id": "1"})
        result = asyncio.get_event_loop().run_until_complete(server.handle_message(msg))
        data = json.loads(result)
        assert len(data["result"]["resources"]) == 1

    def test_handle_resources_read(self) -> None:
        from sovereign_claw.mcp_server import MCPResource, MCPServer

        server = MCPServer()
        server.add_resource(
            MCPResource(uri="file:///a", name="a", content="hello", mime_type="text/plain")
        )
        msg = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "resources/read",
                "params": {"uri": "file:///a"},
                "id": "1",
            }
        )
        result = asyncio.get_event_loop().run_until_complete(server.handle_message(msg))
        data = json.loads(result)
        assert data["result"]["contents"][0]["text"] == "hello"

    def test_handle_resources_read_not_found(self) -> None:
        from sovereign_claw.mcp_server import MCPServer

        server = MCPServer()
        msg = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "resources/read",
                "params": {"uri": "file:///missing"},
                "id": "1",
            }
        )
        result = asyncio.get_event_loop().run_until_complete(server.handle_message(msg))
        data = json.loads(result)
        assert "error" in str(data["result"])

    def test_handle_tools_call(self) -> None:
        from sovereign_claw.mcp_server import MCPServer, MCPTool

        server = MCPServer()
        server.add_tool(MCPTool(name="add", description="Add", handler=lambda a=0, b=0: a + b))
        msg = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "add", "arguments": {"a": 3, "b": 4}},
                "id": "1",
            }
        )
        result = asyncio.get_event_loop().run_until_complete(server.handle_message(msg))
        data = json.loads(result)
        assert "7" in str(data["result"]["content"])

    def test_handle_prompts_list(self) -> None:
        from sovereign_claw.mcp_server import MCPPrompt, MCPServer

        server = MCPServer()
        server.add_prompt(MCPPrompt(name="p1", description="d1"))
        msg = json.dumps({"jsonrpc": "2.0", "method": "prompts/list", "params": {}, "id": "1"})
        result = asyncio.get_event_loop().run_until_complete(server.handle_message(msg))
        data = json.loads(result)
        assert len(data["result"]["prompts"]) == 1

    def test_handle_prompts_get(self) -> None:
        from sovereign_claw.mcp_server import MCPPrompt, MCPServer

        server = MCPServer()
        server.add_prompt(MCPPrompt(name="greet", template="Hello {name}"))
        msg = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "prompts/get",
                "params": {"name": "greet"},
                "id": "1",
            }
        )
        result = asyncio.get_event_loop().run_until_complete(server.handle_message(msg))
        data = json.loads(result)
        assert "Hello {name}" in str(data["result"])

    def test_handle_prompts_get_not_found(self) -> None:
        from sovereign_claw.mcp_server import MCPServer

        server = MCPServer()
        msg = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "prompts/get",
                "params": {"name": "missing"},
                "id": "1",
            }
        )
        result = asyncio.get_event_loop().run_until_complete(server.handle_message(msg))
        data = json.loads(result)
        assert "error" in str(data["result"])

    def test_handle_unknown_method(self) -> None:
        from sovereign_claw.mcp_server import MCPServer

        server = MCPServer()
        msg = json.dumps({"jsonrpc": "2.0", "method": "unknown/method", "params": {}, "id": "1"})
        result = asyncio.get_event_loop().run_until_complete(server.handle_message(msg))
        data = json.loads(result)
        assert "error" in data

    def test_tool_schema(self) -> None:
        from sovereign_claw.mcp_server import MCPTool, MCPToolParam

        tool = MCPTool(
            name="search",
            description="Search",
            parameters=[
                MCPToolParam(
                    name="query", type="string", description="Search query", required=True
                ),
                MCPToolParam(
                    name="limit", type="integer", description="Max results", required=False
                ),
            ],
        )
        schema = tool.schema()
        assert "query" in schema["properties"]
        assert "query" in schema["required"]
        assert "limit" not in schema["required"]

    def test_stats(self) -> None:
        from sovereign_claw.mcp_server import MCPServer, MCPTool

        server = MCPServer()
        server.add_tool(MCPTool(name="t1", description="d1"))
        stats = server.stats()
        assert stats["tools"] == 1
        assert stats["name"] == "sovereign-claw"


# ── Config extended tests ────────────────────────────────────────────────────
class TestConfigExtended:
    def test_env_override_nested(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sovereign_claw.config import load_config

        monkeypatch.setenv("SOVEREIGN_LOG_LEVEL", "DEBUG")
        cfg = load_config()
        assert cfg.log_level == "DEBUG"

    def test_env_override_does_not_destroy_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from sovereign_claw.config import load_config

        monkeypatch.setenv("SOVEREIGN_GATEWAY", "true")
        cfg = load_config()
        # gateway config should remain a dict-like object, not become True
        assert hasattr(cfg, "gateway")


# ── Gateway extended tests ───────────────────────────────────────────────────
class TestGatewayExtended:
    def test_handle_command(self) -> None:
        from sovereign_claw.gateway import Gateway

        gw = Gateway()
        s = gw.create_session()
        msg = json.dumps({"type": "command", "payload": {"command": "status"}})
        result = asyncio.get_event_loop().run_until_complete(gw.handle_message(s.session_id, msg))
        assert result is not None

    def test_close_nonexistent_session(self) -> None:
        from sovereign_claw.gateway import Gateway

        gw = Gateway()
        assert not gw.close_session("nonexistent")

    def test_publish_no_subscribers(self) -> None:
        from sovereign_claw.gateway import Gateway

        gw = Gateway()
        count = gw.publish("topic_with_no_subs", {"data": "hello"})
        assert count == 0
