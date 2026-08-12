"""
graph_elve.py — LangGraph ELFE v∞.1 Loop
========================================
Encodes the full Rabbit → Cypher → Router → Giles / STALL flow
over a LangGraph StateGraph.

Issue #17 de-authorization notice:
  This module CANNOT emit ``ISOMORPHIC_CLOSURE`` as a production authority.
  Any ``ISOMORPHIC_CLOSURE`` label produced by the legacy GILES node is
  non-authoritative and must not be used as a production closure decision.
    - No independent postcondition evaluator assesses before/after state.
    - apply_drift_update() is the synthetic legacy descent (not measured drift).
    - ``giles_node`` unconditionally sets ``state.status = "UNVERIFIED_CONVERGENCE"``;
      the ``ISOMORPHIC_CLOSURE`` string is never emitted from this module.

  Full migration of this module to ConstraintEvaluatorRegistry /
  DriftVectorV1 / ClosureDecisionV1 is tracked by issue #39.
  Until #39 is implemented, this module produces only ``UNVERIFIED_CONVERGENCE``
  or ``T_MAX_VIOLATION_STALL``; no path through this module yields authoritative
  closure.
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from .backends_giles import GilesTiered, GilesTieredConfig, ProviderConfig
from .backends_ollama import CypherOllama, RabbitOllama
from .ip_shield import seal_with_build_fingerprint
from .lanes import LaneRouter as _LaneRouter
from .proof_vault import ProofVault, StepRecord
from .thermodynamics import SystemThermodynamics, TaskManifold

Lane = Literal["RABBIT", "CYPHER", "GILES", "STALL"]

_lane_defaults = _LaneRouter.__init__.__defaults__
if _lane_defaults and len(_lane_defaults) > 0 and _lane_defaults[0] is not None:
    MAX_LOOPS = int(_lane_defaults[0])
else:
    MAX_LOOPS = 2

_rabbit: RabbitOllama | None = None
_cypher: CypherOllama | None = None


def _get_rabbit() -> RabbitOllama:
    global _rabbit
    if _rabbit is None:
        _rabbit = RabbitOllama()
    return _rabbit


def _get_cypher() -> CypherOllama:
    global _cypher
    if _cypher is None:
        _cypher = CypherOllama()
    return _cypher


def _build_giles() -> GilesTiered:
    cfg = GilesTieredConfig(
        primary=ProviderConfig(
            name="anthropic",
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            model=os.environ.get("GILES_MODEL", "claude-opus-4-6"),
        ),
        secondary=(
            ProviderConfig(
                name="openai",
                api_key=os.environ.get("OPENAI_API_KEY", ""),
                model="gpt-4.1-mini",
            )
            if os.environ.get("OPENAI_API_KEY")
            else None
        ),
        tertiary=(
            ProviderConfig(
                name="gemini",
                api_key=os.environ.get("GEMINI_API_KEY", ""),
                model="gemini-1.5-pro",
            )
            if os.environ.get("GEMINI_API_KEY")
            else None
        ),
    )
    return GilesTiered(cfg)


@dataclass
class ELFEState:
    objective: str
    manifold: TaskManifold
    loop_count: int = 0
    draft: str = ""
    critiques: list[str] = field(default_factory=list)
    approved: bool = False
    lane: Lane = "RABBIT"
    drift: float = 1.0
    history: list[dict[str, Any]] = field(default_factory=list)
    trace_id: str | None = None
    done: bool = False
    status: str = "INIT"
    _therm: SystemThermodynamics | None = field(default=None, repr=False, compare=False)

    def get_therm(self) -> SystemThermodynamics:
        if self._therm is None:
            self._therm = SystemThermodynamics(self.manifold)
            self._therm.current_drift = self.drift
        return self._therm


def rabbit_node(state: ELFEState) -> ELFEState:
    rabbit = _get_rabbit()
    decision = rabbit.decide_next_action(
        objective=state.objective,
        history=state.history,
        forbidden_actions=state.manifold.forbidden_actions,
        drift=state.drift,
    )
    state.draft = decision.get("comment", "")
    state.history.append({"lane": "RABBIT", "decision": decision})
    state.lane = "CYPHER"
    return state


def cypher_node(state: ELFEState) -> ELFEState:
    cypher = _get_cypher()
    decision = cypher.decide_next_action(
        objective=state.objective,
        history=state.history,
        forbidden_actions=state.manifold.forbidden_actions,
        drift=state.drift,
    )
    comment = decision.get("comment", "")
    state.critiques.append(comment)
    state.approved = "ok" in comment.lower()
    state.history.append({"lane": "CYPHER", "decision": decision})
    state.loop_count += 1
    return state


def router_node(state: ELFEState) -> ELFEState:
    if state.approved:
        state.lane = "GILES"
    elif state.loop_count < MAX_LOOPS:
        state.lane = "RABBIT"
    else:
        state.lane = "STALL"
    return state


def giles_node(state: ELFEState) -> ELFEState:
    giles = _build_giles()
    envelope = {
        "objective": state.objective,
        "draft": state.draft,
        "critiques": state.critiques,
        "loop_count": state.loop_count,
    }
    decision = giles.decide_next_action(
        objective=str(envelope),
        history=state.history,
        forbidden_actions=state.manifold.forbidden_actions,
        drift=state.drift,
    )
    state.history.append({"lane": "GILES", "decision": decision})

    vault = ProofVault()
    trace_meta = seal_with_build_fingerprint(
        {
            "lane": "AUTHORITATIVE",
            "loop_count": state.loop_count,
        }
    )
    trace_id = vault.create_trace(
        objective=state.objective,
        meta=trace_meta,
    )
    vault.append_step(
        StepRecord(
            trace_id=trace_id,
            step_index=0,
            timestamp=time.time(),
            node="GILES",
            action="GATA_PRIME_SEAL",
            drift=state.drift,
            # [DE-AUTHORIZED] Non-authoritative legacy label pending #39 migration.
            status="UNVERIFIED_CONVERGENCE",
            payload={"envelope": envelope, "decision": decision},
        )
    )

    state.trace_id = trace_id
    state.done = True
    # [DE-AUTHORIZED as production closure — issue #17 / migrate in #39]
    # This path uses model/approval without measured ConstraintAssessmentV1 evidence.
    # The vault step is logged with the non-authoritative label; the public status
    # is set to UNVERIFIED_CONVERGENCE so no alternate public path can emit
    # synthetic ISOMORPHIC_CLOSURE authority from this module.
    state.status = "UNVERIFIED_CONVERGENCE"
    return state


def stall_guard_node(state: ELFEState) -> ELFEState:
    state.done = True
    state.status = "T_MAX_VIOLATION_STALL"
    return state


def elfe_superstep(state: ELFEState) -> ELFEState:
    therm = state.get_therm()

    if state.lane == "RABBIT":
        state = rabbit_node(state)
    elif state.lane == "CYPHER":
        state = cypher_node(state)
    elif state.lane == "GILES":
        state = giles_node(state)
    elif state.lane == "STALL":
        state = stall_guard_node(state)
    else:
        state.done = True
        state.status = "INVALID_LANE"
        return state

    if not state.done and state.lane in ("RABBIT", "CYPHER"):
        state = router_node(state)

    # [LEGACY path — issue #17] apply_drift_update is the synthetic descent surrogate.
    # Production authority migrated to DriftVectorV1/update_from_measured_vector.
    # Full graph migration tracked by #39.
    therm.apply_drift_update(step_count=state.loop_count, error_penalty=0.0)
    state.drift = therm.current_drift

    ts = therm.check_isomorphic_state(step_count=state.loop_count)
    if ts == "T_MAX_VIOLATION" and not state.done:
        state.done = True
        state.status = "HALTED_SILENCE_CLAUSE"

    return state


def build_elve_graph():
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise ImportError(
            "langgraph is required for build_elve_graph(). "
            "Install with: pip install langgraph>=0.2.0"
        ) from exc

    graph = StateGraph(ELFEState)

    def node_fn(state: ELFEState) -> dict[str, Any]:
        new_state = elfe_superstep(state)
        d = asdict(new_state)
        d.pop("_therm", None)
        return d

    def should_continue(state: dict[str, Any]) -> str:
        return END if state.get("done") else "ELFE_STEP"

    graph.add_node("ELFE_STEP", node_fn)
    graph.add_edge(START, "ELFE_STEP")
    graph.add_conditional_edges(
        "ELFE_STEP",
        should_continue,
        {END: END, "ELFE_STEP": "ELFE_STEP"},
    )

    return graph.compile()
