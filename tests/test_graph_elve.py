from __future__ import annotations

import sys
import types

import pytest

from sovereign_claw.graph_elve import (
    ELFEState,
    build_elve_graph,
    cypher_node,
    elfe_superstep,
    giles_node,
    rabbit_node,
    router_node,
    stall_guard_node,
)
from sovereign_claw.thermodynamics import TaskManifold


def make_state(**overrides):
    state = ELFEState(
        objective="test objective",
        manifold=TaskManifold(objective="test objective"),
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


class DummyRabbit:
    def decide_next_action(self, **kwargs):
        return {"comment": "rabbit draft"}


class DummyCypherApprove:
    def decide_next_action(self, **kwargs):
        return {"comment": "ok to proceed"}


class DummyCypherReject:
    def decide_next_action(self, **kwargs):
        return {"comment": "needs revision"}


class DummyGiles:
    def decide_next_action(self, **kwargs):
        return {"comment": "sealed authoritative output"}


class DummyVault:
    def __init__(self):
        self.created = []
        self.steps = []

    def create_trace(self, objective, meta):
        self.created.append({"objective": objective, "meta": meta})
        return "trace-123"

    def append_step(self, step):
        self.steps.append(step)


def test_rabbit_node_sets_draft_and_advances_lane(monkeypatch):
    monkeypatch.setattr("sovereign_claw.graph_elve._get_rabbit", lambda: DummyRabbit())
    state = make_state()

    new_state = rabbit_node(state)

    assert new_state.draft == "rabbit draft"
    assert new_state.lane == "CYPHER"
    assert new_state.history[-1]["lane"] == "RABBIT"


def test_cypher_node_approves_and_increments_loop(monkeypatch):
    monkeypatch.setattr("sovereign_claw.graph_elve._get_cypher", lambda: DummyCypherApprove())
    state = make_state(history=[{"lane": "RABBIT"}])

    new_state = cypher_node(state)

    assert new_state.approved is True
    assert new_state.loop_count == 1
    assert new_state.critiques[-1] == "ok to proceed"
    assert new_state.history[-1]["lane"] == "CYPHER"


def test_cypher_node_rejects_when_comment_not_ok(monkeypatch):
    monkeypatch.setattr("sovereign_claw.graph_elve._get_cypher", lambda: DummyCypherReject())
    state = make_state()

    new_state = cypher_node(state)

    assert new_state.approved is False
    assert new_state.loop_count == 1
    assert new_state.critiques[-1] == "needs revision"


def test_router_node_goes_to_giles_when_approved():
    state = make_state(approved=True, loop_count=1, lane="CYPHER")

    new_state = router_node(state)

    assert new_state.lane == "GILES"


def test_router_node_loops_back_to_rabbit_when_not_approved_and_loops_remaining():
    state = make_state(approved=False, loop_count=0, lane="CYPHER")

    new_state = router_node(state)

    assert new_state.lane == "RABBIT"


def test_router_node_stalls_when_max_loops_reached():
    state = make_state(approved=False, loop_count=99, lane="CYPHER")

    new_state = router_node(state)

    assert new_state.lane == "STALL"


def test_giles_node_seals_trace_and_completes(monkeypatch):
    vault = DummyVault()

    monkeypatch.setattr("sovereign_claw.graph_elve._build_giles", lambda: DummyGiles())
    monkeypatch.setattr("sovereign_claw.graph_elve.ProofVault", lambda: vault)
    monkeypatch.setattr(
        "sovereign_claw.graph_elve.seal_with_build_fingerprint",
        lambda meta: {"sealed": True, **meta},
    )

    state = make_state(
        draft="draft text",
        critiques=["critique 1"],
        loop_count=2,
        history=[],
    )

    new_state = giles_node(state)

    assert new_state.done is True
    # graph_elve.py emits UNVERIFIED_CONVERGENCE (non-authoritative legacy label per #39)
    assert new_state.status == "UNVERIFIED_CONVERGENCE"
    assert new_state.trace_id == "trace-123"
    assert vault.created[0]["objective"] == "test objective"
    assert vault.created[0]["meta"]["sealed"] is True
    assert vault.steps[0].node == "GILES"
    assert vault.steps[0].action == "GATA_PRIME_SEAL"
    assert new_state.history[-1]["lane"] == "GILES"


def test_stall_guard_node_marks_terminal():
    state = make_state()

    new_state = stall_guard_node(state)

    assert new_state.done is True
    assert new_state.status == "T_MAX_VIOLATION_STALL"


def test_elfe_superstep_handles_invalid_lane():
    state = make_state(lane="BROKEN")

    new_state = elfe_superstep(state)

    assert new_state.done is True
    assert new_state.status == "INVALID_LANE"


def test_elfe_superstep_rabbit_then_routes(monkeypatch):
    monkeypatch.setattr("sovereign_claw.graph_elve._get_rabbit", lambda: DummyRabbit())
    state = make_state(lane="RABBIT")

    new_state = elfe_superstep(state)

    assert new_state.draft == "rabbit draft"
    assert new_state.lane == "RABBIT"
    assert new_state.done is False
    assert 0.0 <= new_state.drift <= 1.0


def test_elfe_superstep_cypher_approved_routes_to_giles(monkeypatch):
    monkeypatch.setattr("sovereign_claw.graph_elve._get_cypher", lambda: DummyCypherApprove())
    state = make_state(lane="CYPHER", history=[{"lane": "RABBIT"}])

    new_state = elfe_superstep(state)

    assert new_state.approved is True
    assert new_state.lane == "GILES"
    assert new_state.done is False


def test_elfe_superstep_stall_lane_terminates():
    state = make_state(lane="STALL")

    new_state = elfe_superstep(state)

    assert new_state.done is True
    assert new_state.status == "T_MAX_VIOLATION_STALL"


def test_elfe_superstep_halts_on_tmax_violation(monkeypatch):
    class DummyTherm:
        def __init__(self):
            self.current_drift = 0.4

        def apply_drift_update(self, step_count, error_penalty):
            self.current_drift = 0.4

        def check_isomorphic_state(self, step_count):
            return "T_MAX_VIOLATION"

    monkeypatch.setattr("sovereign_claw.graph_elve._get_rabbit", lambda: DummyRabbit())
    state = make_state(lane="RABBIT")
    state._therm = DummyTherm()

    new_state = elfe_superstep(state)

    assert new_state.done is True
    assert new_state.status == "HALTED_SILENCE_CLAUSE"


def test_build_elve_graph_import_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "langgraph.graph":
            raise ImportError("missing langgraph")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(ImportError, match="langgraph is required"):
        build_elve_graph()


def test_build_elve_graph_with_fake_langgraph(monkeypatch):
    class FakeCompiledGraph:
        pass

    class FakeStateGraph:
        def __init__(self, state_type):
            self.state_type = state_type
            self.nodes = {}
            self.edges = []
            self.conditional = None

        def add_node(self, name, fn):
            self.nodes[name] = fn

        def add_edge(self, start, end):
            self.edges.append((start, end))

        def add_conditional_edges(self, node_name, should_continue, mapping):
            self.conditional = (node_name, should_continue, mapping)

        def compile(self):
            return FakeCompiledGraph()

    fake_module = types.SimpleNamespace(
        StateGraph=FakeStateGraph,
        END="END",
        START="START",
    )

    monkeypatch.setitem(sys.modules, "langgraph", types.ModuleType("langgraph"))
    monkeypatch.setitem(sys.modules, "langgraph.graph", fake_module)

    compiled = build_elve_graph()

    assert isinstance(compiled, FakeCompiledGraph)
