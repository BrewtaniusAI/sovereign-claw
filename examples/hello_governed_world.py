#!/usr/bin/env python3
"""
hello_governed_world.py — Canonical End-to-End Governed Execution Demo
======================================================================

Demonstrates the complete governance flow:
  1. PolicyEngine evaluates the objective
  2. Orchestrator executes under governance (T_max, drift, forbidden actions)
  3. ProofVault records every step to immutable audit log
  4. ReceiptBuilder exports a verifiable proof receipt
  5. DriftTracker decomposes drift into tool/constraint/provider/policy

This is the "hello world" for Sovereign Claw — showing that every
execution is policy-gated, proof-vaulted, and drift-tracked.

Usage:
    python -m examples.hello_governed_world
    # or
    python examples/hello_governed_world.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure the package is importable when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sovereign_claw.orchestrator import Orchestrator
from sovereign_claw.thermodynamics import TaskManifold
from sovereign_claw.policy_engine import PolicyEngine, PolicyProfile
from sovereign_claw.receipts import ReceiptBuilder
from sovereign_claw.drift import DriftTracker
from sovereign_claw.memory import MemoryStore


# ── Step 0: Define a governed tool ───────────────────────────────────────────
def greet(name: str) -> str:
    """A simple governed tool — even 'hello world' is audited."""
    return f"Hello, {name}! Welcome to the governed world."


# ── Step 1: Configure the governance stack ───────────────────────────────────
class HelloBackend:
    """
    Minimal LLM backend for the demo. Produces a deterministic
    sequence: greet → HALT. No real inference needed.
    """

    def __init__(self) -> None:
        self._step = 0

    def decide_next_action(
        self,
        objective: str,
        history: list[dict[str, object]],
        forbidden_actions: list[str],
        drift: float,
    ) -> dict[str, object]:
        self._step += 1

        if self._step == 1:
            return {
                "tool": "greet",
                "kwargs": {"name": "Governed World"},
                "comment": "first governed action",
                "agent_id": "hello_backend",
            }

        return {
            "tool": "HALT",
            "kwargs": {},
            "comment": "objective achieved — closing under governance",
            "agent_id": "hello_backend",
        }


def main() -> int:
    print("=" * 60)
    print("  Sovereign Claw — Hello Governed World")
    print("  End-to-End Governance Demo")
    print("=" * 60)

    # ── 1. Policy Gate ────────────────────────────────────────────────────
    print("\n[1/6] Policy Evaluation")
    engine = PolicyEngine(
        forbidden_tools=["dangerous_tool"],
        profile=PolicyProfile.BALANCED,
    )

    # Test: our tool is allowed
    decision = engine.evaluate({"tool": "greet", "trace_id": "demo-001"})
    print(f"  Tool 'greet' allowed: {decision.allowed}")
    print(f"  Profile: {decision.profile}")

    # Test: forbidden tool is blocked
    blocked = engine.evaluate({"tool": "dangerous_tool", "trace_id": "demo-001"})
    print(f"  Tool 'dangerous_tool' allowed: {blocked.allowed}")
    print(f"  Reasons: {blocked.reasons}")
    print(f"  Matched policies: {blocked.matched_policies}")

    # ── 2. Governed Execution ─────────────────────────────────────────────
    print("\n[2/6] Governed Execution (Orchestrator)")
    backend = HelloBackend()
    orchestrator = Orchestrator(
        llm_backend=backend,
        tools={"greet": greet},
    )

    manifold = TaskManifold(
        objective="Say hello to the governed world",
        forbidden_actions=["dangerous_tool"],
        t_max_steps=5,
    )

    result = orchestrator.execute(manifold)
    trace_id = result.trace_id
    print(f"  Trace ID: {trace_id}")
    print(f"  Status: {result.status}")
    print(f"  Steps: {result.steps}")
    print(f"  Final drift: {result.final_drift:.4f}")

    # ── 3. ProofVault Audit ───────────────────────────────────────────────
    print("\n[3/6] ProofVault Audit Trail")
    vault = orchestrator.vault
    summary = vault.get_trace_summary(trace_id)
    print(f"  Trace summary: {json.dumps(summary, indent=4, default=str)}")

    # ── 4. Proof Receipt Export ───────────────────────────────────────────
    print("\n[4/6] Proof Receipt (Hash Chain)")
    builder = ReceiptBuilder(vault)
    receipt_json = builder.export(trace_id, fmt="json")
    print(f"  Receipt (JSON):\n{receipt_json}")

    receipt_hash = builder.export(trace_id, fmt="hash")
    print(f"\n  Receipt (Hash Digest):\n{receipt_hash}")

    # Verify the chain
    receipt = builder.build_receipt(trace_id)
    is_valid = builder.verify_chain(receipt)
    print(f"\n  Chain verified: {is_valid}")

    # ── 5. Replay ─────────────────────────────────────────────────────────
    print("\n[5/6] Execution Replay")
    steps = builder.replay(trace_id)
    for step in steps:
        drift_dir = "+" if step.drift_delta >= 0 else ""
        print(
            f"  [{step.step_index}] {step.action} "
            f"drift={step.drift:.4f} ({drift_dir}{step.drift_delta:.4f}) "
            f"tool={step.tool} success={step.success}"
        )
        if step.comment:
            print(f"         {step.comment}")

    # ── 6. Drift Decomposition ────────────────────────────────────────────
    print("\n[6/6] Drift Decomposition")
    tracker = DriftTracker(trace_id)
    tracker.record_tool_drift(0, 0.05, "greet tool executed")
    tracker.record_constraint_drift(0, 0.02, "constraint projection applied")
    tracker.record_provider_drift(0, 0.0, "demo backend — zero provider drift")
    tracker.record_policy_drift(0, 0.01, "balanced profile gate")

    report = tracker.report()
    summary_data = report.summary()
    print(f"  Total drift: {summary_data['total_drift']:.4f}")
    print(f"  Dominant source: {summary_data['dominant_source']}")
    print("  Breakdown:")
    for key, val in summary_data["breakdown"].items():
        print(f"    {key}: {val:.4f}")

    # ── 7. Memory Store (bonus) ───────────────────────────────────────────
    print("\n[Bonus] Memory Store")
    memory = MemoryStore()
    memory.store(
        content="Executed hello governed world demo",
        memory_type="episodic",
        trace_id=trace_id,
        tags=["demo", "hello_world"],
    )
    stats = memory.stats()
    print(f"  Entries stored: {stats.total_entries}")
    print(f"  Episodic: {stats.episodic_count}")

    print("\n" + "=" * 60)
    print("  Demo complete. Every action was:")
    print("    - Policy-gated (PolicyEngine)")
    print("    - Governed (Orchestrator with T_max + drift)")
    print("    - Proof-vaulted (ProofVault)")
    print("    - Receipt-exportable (ReceiptBuilder)")
    print("    - Drift-decomposed (DriftTracker)")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
