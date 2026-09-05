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

CHANGELOG v3.3.0
-----------------
- MEDIA    : Added media pipeline (image/audio/video processing, transcription hooks, size caps, temp lifecycle)
- WEBTOOLS : Added web search & fetch (multi-provider search, URL content extraction, result dedup)
- CONTEXT  : Added context engine (token-budget-aware context management, compaction, snapshots)
- PLUGIN   : Added plugin SDK (manifest system, lifecycle, dependency resolution, sandboxing, trust scores)
- USAGE    : Added usage tracking (per-session token/cost tracking, budget alerts, daily limits)
- COMMANDS : Added chat commands (in-channel /status, /new, /reset, /compact, /think, /verbose, /usage)
- SECRETS  : Added secrets manager (encrypted-at-rest, scoped access, rotation, audit trail)
- VERSION  : Bumped to 3.3.0 across __init__, pyproject.toml

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

from .a2a import A2AServer, A2ATask, TaskState
from .a2a import AgentCard as A2AAgentCard
from .chat_commands import ChatCommandRegistry, CommandDefinition, CommandResult, parse_command
from .context_engine import CompactionStrategy, ContextEngine, ContextMessage, TokenBudget
from .drift import DriftBreakdown, DriftComponent, DriftReport, DriftTracker
from .event_stream import EventRecord, EventStream
from .gardeners_protocol import GardenersProtocol, SessionRecord, SkillScroll
from .guardrails import GuardrailDecision, GuardrailEngine, GuardrailRule, GuardrailSeverity
from .ip_shield import BUILD_FINGERPRINT, load_elfe_coefficients
from .kitaev_shield import KitaevZeroMode
from .lanes import Lane, LaneRouter

# v3.3.0 platform completeness (static imports so CodeQL can resolve exports)
from .media_pipeline import MediaArtifact, MediaPipeline, MediaSizeCap, MediaType
from .memory import MemoryEntry, MemoryQuery, MemoryStats, MemoryStore
from .measured_closure import (
    ClosureDecision,
    ComponentMeasurement,
    ConstraintAssessment,
    EvaluatorRegistry,
    MeasurementState,
    StabilityCertificate,
    TrustedCertificateRegistry,
    VerifiedComponentEvidenceV1,
    VerifiedEvidenceBindingV1,
    evaluate_closure,
)
from .multi_agent import (
    AgentCard,
    AgentRegistry,
    AgentRole,
    MultiAgentOrchestrator,
)
from .mythic_neuro_kernel import DongbaGlyph, MythicNeuroKernel
from .orchestrator import ExecutionReceipt, LLMBackend, Orchestrator
from .persistent_memory import PersistentMemoryStore
from .plugin_sdk import PluginHook, PluginManifest, PluginPermission, PluginSandbox, PluginSDK
from .policy_engine import PolicyDecision, PolicyEngine, PolicyProfile
from .proof_vault import (
    ChainVerificationResult,
    EvidenceRecord,
    LedgerIntegrityError,
    ProofVault,
    StepRecord,
    canonical_json,
)
from .receipts import HashedStep, ProofReceipt, ReceiptBuilder, ReplayStep, TraceDiff
from .runtime import SovereignRuntime
from .secrets_manager import SecretMetadata, SecretScope, SecretsManager
from .thermodynamics import SystemThermodynamics, TaskManifold
from .usage_tracking import BudgetConfig, ProviderRates, UsageRecord, UsageTracker
from .weavers_kernel import AccelerationReceipt, WeaversKernel
from .web_tools import ContentFetcher, FetchedContent, SearchResponse, SearchResult, WebSearchEngine

__version__ = "3.3.0"


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
    "EvidenceRecord",
    "ChainVerificationResult",
    "LedgerIntegrityError",
    "canonical_json",
    "KitaevZeroMode",
    "Lane",
    "LaneRouter",
    "ClosureDecision",
    "ComponentMeasurement",
    "ConstraintAssessment",
    "EvaluatorRegistry",
    "MeasurementState",
    "StabilityCertificate",
    "TrustedCertificateRegistry",
    "VerifiedComponentEvidenceV1",
    "VerifiedEvidenceBindingV1",
    "evaluate_closure",
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
    # v3.3.0 platform completeness
    "MediaPipeline",
    "MediaType",
    "MediaArtifact",
    "MediaSizeCap",
    "WebSearchEngine",
    "ContentFetcher",
    "SearchResult",
    "SearchResponse",
    "FetchedContent",
    "ContextEngine",
    "TokenBudget",
    "ContextMessage",
    "CompactionStrategy",
    "PluginSDK",
    "PluginManifest",
    "PluginPermission",
    "PluginHook",
    "PluginSandbox",
    "UsageTracker",
    "BudgetConfig",
    "ProviderRates",
    "UsageRecord",
    "ChatCommandRegistry",
    "CommandDefinition",
    "CommandResult",
    "parse_command",
    "SecretsManager",
    "SecretScope",
    "SecretMetadata",
]
