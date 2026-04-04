"""
sovereign_claw — Governed Sovereign Agent Runtime
==================================================
Deterministic, constraint-first agent runtime with proof-vaulted execution.

Public API
----------
    from sovereign_claw import Orchestrator, TaskManifold, ProofVault, SovereignRuntime

Advanced
--------
    from sovereign_claw.kitaev_shield import KitaevZeroMode
    from sovereign_claw.thermodynamics import SystemThermodynamics
    from sovereign_claw.lanes import Lane, LaneRouter
    from sovereign_claw.graph_elve import build_elve_graph, ELFEState
    from sovereign_claw.backends_giles import GilesTiered, GilesTieredConfig
    from sovereign_claw.backends_ollama import RabbitOllama, CypherOllama

Governance
----------
    CollectiveOS · GOD FILE v∞.1 · Isomorphic Closure · Fixed-Time Stability
    © Brewtanius Ink LLC / Immortal Tek Inc.  All rights reserved.

CHANGELOG v3.2.0
-----------------
- LOGGING  : Added structured logging (JSON/human/compact formatters, correlation IDs, trace context)
- RATELIM  : Added token bucket rate limiter (per-key/channel/provider/tool, sliding window)
- HEALTH   : Added health check API (liveness, readiness, component status)
- WEBHOOK  : Added webhook receiver (HMAC-SHA256 verification, event routing, replay protection)
- EVENTBUS : Added governed event bus (pub/sub, typed events, priority ordering, dead letter queue)
- README   : Synced to v3.2.0 (badges, module tables, capability matrix)
- ARCH     : Updated ARCHITECTURE.md with Production Infrastructure section
- VERSION  : Bumped to 3.2.0 across __init__, pyproject.toml

CHANGELOG v3.1.0
-----------------
- CONFIG   : Migrated config.py from dataclasses to Pydantic v2 (field validators, env-var parsing, .env support)
- A2A      : Added Agent2Agent protocol (agent cards, task lifecycle, state machine)
- GUARD    : Added Autonomous Guardrails engine (privilege escalation, loop detection, destructive action gating, cost/token limits)
- MEMORY   : Added PersistentMemoryStore (SQLite-backed episodic/semantic/task memory with TTL)
- POLICY   : Fixed test_policy() shallow copy bug (ViolationRecord mutation leakage)
- DOCKER   : Added Dockerfile + docker-compose.yml for containerized deployment
- VERSION  : Bumped to 3.1.0 across __init__, pyproject.toml

CHANGELOG v3.0.0
-----------------
- DRIFT-1  : Version aligned to 3.0.0 across __init__, pyproject.toml, build_protected.py
- DRIFT-2  : Removed duplicate lane_router.advance() call in WeaversKernel.accelerate()
- DRIFT-3  : WeaversKernel now calls seal_with_build_fingerprint() on every vault trace
- DRIFT-4  : giles_node() timestamp fixed from hardcoded 0.0 to time.time()
- DRIFT-5  : ELFEState._therm persistence fixed; get_therm() no longer resets drift history
- DRIFT-6  : Orchestrator.execute() injects build fingerprint into vault trace meta
- DRIFT-7  : graph_elve.MAX_LOOPS synchronised with LaneRouter default via shared constant
- DRIFT-8  : ip_shield key decoding documented as placeholder; validation hardened
- DRIFT-9  : Confirmed GilesTiered.decide_next_action() injects agent_id (already correct)
- DRIFT-10 : tools_basic.py gains ToolSpec dataclass + __all__ registry
- DRIFT-11 : MythicNeuroKernel loads ELFE coefficients from ip_shield.load_elfe_coefficients()
- DRIFT-12 : Added example 04_kitaev_penalty_tiers.py to close sequence gap
- DRIFT-13 : GardenersProtocol scroll state transitions now emit event log entries
"""

from .orchestrator import Orchestrator, ExecutionReceipt, LLMBackend
from .thermodynamics import TaskManifold, SystemThermodynamics
from .proof_vault import ProofVault, StepRecord
from .kitaev_shield import KitaevZeroMode
from .lanes import Lane, LaneRouter
from .weavers_kernel import WeaversKernel, AccelerationReceipt
from .mythic_neuro_kernel import MythicNeuroKernel, DongbaGlyph
from .gardeners_protocol import GardenersProtocol, SkillScroll, SessionRecord
from .ip_shield import BUILD_FINGERPRINT, load_elfe_coefficients
from .event_stream import EventStream, EventRecord
from .policy_engine import PolicyEngine, PolicyDecision, PolicyProfile
from .runtime import SovereignRuntime
from .receipts import ReceiptBuilder, ProofReceipt, HashedStep, ReplayStep, TraceDiff
from .drift import DriftTracker, DriftBreakdown, DriftComponent, DriftReport
from .memory import MemoryStore, MemoryEntry, MemoryQuery, MemoryStats
from .multi_agent import (
    AgentRole,
    AgentCard,
    AgentRegistry,
    MultiAgentOrchestrator,
)
from .a2a import A2AServer, A2ATask, AgentCard as A2AAgentCard, TaskState
from .guardrails import GuardrailEngine, GuardrailRule, GuardrailDecision, GuardrailSeverity
from .persistent_memory import PersistentMemoryStore


__version__ = "3.2.0"


# ── v3.0.0 platform modules (lazy imports to keep startup fast) ──────────────
def __getattr__(name: str):  # type: ignore[no-untyped-def]
    _lazy = {
        "SovereignConfig": ".config",
        "load_config": ".config",
        "save_config": ".config",
        "ModelRouter": ".model_router",
        "ProviderCost": ".model_router",
        "ExecutionMode": ".model_router",
        "Gateway": ".gateway",
        "GatewaySession": ".gateway",
        "SecurityManager": ".security",
        "SkillsManager": ".skills",
        "SkillSpec": ".skills",
        "Skill": ".skills",
        "VoiceEngine": ".voice",
        "Canvas": ".canvas",
        "BrowserController": ".browser",
        "SessionManager": ".sessions",
        "Scheduler": ".scheduler",
        "MCPServer": ".mcp_server",
        "ChannelMesh": ".channels.mesh",
        "ChannelIdentity": ".channels.mesh",
        "MeshSession": ".channels.mesh",
        "A2AServer": ".a2a",
        "A2ATask": ".a2a",
        "TaskState": ".a2a",
        "GuardrailEngine": ".guardrails",
        "GuardrailRule": ".guardrails",
        "GuardrailDecision": ".guardrails",
        "PersistentMemoryStore": ".persistent_memory",
        "GovernedLogger": ".structured_logging",
        "TraceContext": ".structured_logging",
        "LogFormat": ".structured_logging",
        "configure_logging": ".structured_logging",
        "get_logger": ".structured_logging",
        "set_correlation_id": ".structured_logging",
        "RateLimiter": ".rate_limiter",
        "RateLimitCategory": ".rate_limiter",
        "RateLimitConfig": ".rate_limiter",
        "RateLimitResult": ".rate_limiter",
        "HealthChecker": ".health",
        "HealthStatus": ".health",
        "ComponentHealth": ".health",
        "HealthReport": ".health",
        "WebhookReceiver": ".webhooks",
        "WebhookSource": ".webhooks",
        "WebhookEvent": ".webhooks",
        "WebhookVerificationMethod": ".webhooks",
        "EventBus": ".event_bus",
        "BusEvent": ".event_bus",
        "EventPriority": ".event_bus",
        "EventStatus": ".event_bus",
    }
    if name in _lazy:
        import importlib

        mod = importlib.import_module(_lazy[name], __package__)
        return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Core agent framework
    "Orchestrator",
    "ExecutionReceipt",
    "LLMBackend",
    "TaskManifold",
    "SystemThermodynamics",
    "ProofVault",
    "StepRecord",
    "KitaevZeroMode",
    "Lane",
    "LaneRouter",
    "SovereignRuntime",
    # Human-in-the-loop skill leveling
    "WeaversKernel",
    "AccelerationReceipt",
    "MythicNeuroKernel",
    "DongbaGlyph",
    "GardenersProtocol",
    "SkillScroll",
    "SessionRecord",
    # IP / build
    "BUILD_FINGERPRINT",
    "load_elfe_coefficients",
    "EventStream",
    "EventRecord",
    "PolicyEngine",
    "PolicyDecision",
    "PolicyProfile",
    # Proof receipts + drift + memory
    "ReceiptBuilder",
    "ProofReceipt",
    "HashedStep",
    "ReplayStep",
    "TraceDiff",
    "DriftTracker",
    "DriftBreakdown",
    "DriftComponent",
    "DriftReport",
    "MemoryStore",
    "MemoryEntry",
    "MemoryQuery",
    "MemoryStats",
    # Multi-agent
    "AgentRole",
    "AgentCard",
    "AgentRegistry",
    "MultiAgentOrchestrator",
    # v3.0.0 platform
    "SovereignConfig",
    "load_config",
    "save_config",
    "ModelRouter",
    "Gateway",
    "GatewaySession",
    "SecurityManager",
    "SkillsManager",
    "SkillSpec",
    "Skill",
    "ProviderCost",
    "ExecutionMode",
    "ChannelMesh",
    "ChannelIdentity",
    "MeshSession",
    "VoiceEngine",
    "Canvas",
    "BrowserController",
    "SessionManager",
    "Scheduler",
    "MCPServer",
    # v3.1.0 modules
    "A2AServer",
    "A2ATask",
    "A2AAgentCard",
    "TaskState",
    "GuardrailEngine",
    "GuardrailRule",
    "GuardrailDecision",
    "GuardrailSeverity",
    "PersistentMemoryStore",
    # v3.2.0 production infrastructure
    "GovernedLogger",
    "TraceContext",
    "LogFormat",
    "configure_logging",
    "get_logger",
    "set_correlation_id",
    "RateLimiter",
    "RateLimitCategory",
    "RateLimitConfig",
    "RateLimitResult",
    "HealthChecker",
    "HealthStatus",
    "ComponentHealth",
    "HealthReport",
    "WebhookReceiver",
    "WebhookSource",
    "WebhookEvent",
    "WebhookVerificationMethod",
    "EventBus",
    "BusEvent",
    "EventPriority",
    "EventStatus",
]
