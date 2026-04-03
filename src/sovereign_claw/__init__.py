"""
sovereign_claw — Isomorphic Intelligence Framework
===================================================
The world's first deterministic, thermodynamically governed agent framework.

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

CHANGELOG v2.0.2
----------------
- DRIFT-1  : Version aligned to 2.0.0 across __init__, pyproject.toml, build_protected.py
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
from .policy_engine import PolicyEngine, PolicyDecision
from .runtime import SovereignRuntime

__version__ = "3.0.0"


# ── v3.0.0 platform modules (lazy imports to keep startup fast) ──────────────
def __getattr__(name: str):  # type: ignore[no-untyped-def]
    _lazy = {
        "SovereignConfig": ".config",
        "load_config": ".config",
        "save_config": ".config",
        "ModelRouter": ".model_router",
        "Gateway": ".gateway",
        "GatewaySession": ".gateway",
        "SecurityManager": ".security",
        "SkillsManager": ".skills",
        "VoiceEngine": ".voice",
        "Canvas": ".canvas",
        "BrowserController": ".browser",
        "SessionManager": ".sessions",
        "Scheduler": ".scheduler",
        "MCPServer": ".mcp_server",
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
    # v3.0.0 platform
    "SovereignConfig",
    "load_config",
    "save_config",
    "ModelRouter",
    "Gateway",
    "GatewaySession",
    "SecurityManager",
    "SkillsManager",
    "VoiceEngine",
    "Canvas",
    "BrowserController",
    "SessionManager",
    "Scheduler",
    "MCPServer",
]
