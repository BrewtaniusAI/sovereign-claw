"""
04_kitaev_penalty_tiers.py — Kitaev Zero-Mode Penalty Tiers
============================================================
Demonstrates how KitaevZeroMode classifies tool execution errors into
drift penalty tiers and how those penalties propagate into the
Orchestrator's drift trajectory.

DRIFT-12 FIX (v2.0.0)
----------------------
The example sequence jumped from 03 to 05, leaving a gap in the
documented surface area.  This example closes the gap by showing:

  1. How KitaevZeroMode maps exception types to drift penalties
  2. How a high-penalty error triggers the Soft Silence Clause
  3. How the drift_trajectory in ExecutionReceipt records the event
  4. How to inspect agent reputation weights after a failed run

Run
---
    python examples/04_kitaev_penalty_tiers.py

No external API keys required.  Uses mock LLM and mock tools.
"""
from __future__ import annotations

import sys
import os

# Allow running from repo root without installing
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sovereign_claw import Orchestrator, TaskManifold, ProofVault
from sovereign_claw.kitaev_shield import KitaevZeroMode
from typing import Any, Dict, List


# ── Mock LLM that calls a tool once then halts ────────────────────────────────
class MockLLM:
    """
    Deterministic mock LLM for testing penalty tier propagation.

    Behaviour
    ---------
    Step 0 : calls 'safe_tool'   — succeeds, low drift penalty
    Step 1 : calls 'risky_tool'  — raises ValueError, medium penalty
    Step 2 : calls 'fatal_tool'  — raises RuntimeError, high penalty
    Step 3+: issues HALT
    """
    def __init__(self):
        self._step = 0

    def decide_next_action(
        self,
        objective: str,
        history: List[Dict[str, Any]],
        forbidden_actions: List[str],
        drift: float,
    ) -> Dict[str, Any]:
        step = self._step
        self._step += 1

        if step == 0:
            return {
                "tool":    "safe_tool",
                "kwargs":  {"msg": "hello"},
                "comment": "Calling safe tool — expect low drift.",
                "agent_id": "mock_llm",
            }
        elif step == 1:
            return {
                "tool":    "risky_tool",
                "kwargs":  {},
                "comment": "Calling risky tool — expect ValueError penalty.",
                "agent_id": "mock_llm",
            }
        elif step == 2:
            return {
                "tool":    "fatal_tool",
                "kwargs":  {},
                "comment": "Calling fatal tool — expect RuntimeError penalty.",
                "agent_id": "mock_llm",
            }
        else:
            return {
                "tool":    "HALT",
                "kwargs":  {},
                "comment": "All tiers demonstrated. Issuing HALT.",
                "agent_id": "mock_llm",
            }


# ── Tool implementations ──────────────────────────────────────────────────────
def safe_tool(msg: str) -> str:
    """Returns a message. Never raises."""
    return f"safe_tool OK: {msg}"


def risky_tool() -> str:
    """Raises ValueError — maps to MEDIUM drift penalty in KitaevZeroMode."""
    raise ValueError("Simulated validation error")


def fatal_tool() -> str:
    """Raises RuntimeError — maps to HIGH drift penalty in KitaevZeroMode."""
    raise RuntimeError("Simulated fatal execution error")


# ── Main demo ─────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("Kitaev Zero-Mode Penalty Tier Demonstration")
    print("=" * 60)

    # Use an in-memory DB for this demo
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp()) / "demo_vault.sqlite3"
    vault = ProofVault(db_path=tmp)

    # KitaevZeroMode with default penalty scale
    shield = KitaevZeroMode()

    # Manifold: 10 steps, risk_threshold 0.8 (generous — let all tiers fire)
    manifold = TaskManifold(
        objective="Demonstrate Kitaev penalty tiers",
        t_max_steps=10,
        risk_threshold=0.95,  # high threshold so all 3 tiers execute
        forbidden_actions=[],
    )

    llm = MockLLM()
    orch = Orchestrator(llm_backend=llm, vault=vault, shield=shield)
    orch.register_tool("safe_tool",  safe_tool)
    orch.register_tool("risky_tool", risky_tool)
    orch.register_tool("fatal_tool", fatal_tool)

    receipt = orch.execute(manifold)

    print(f"\nFinal status   : {receipt.status}")
    print(f"Steps executed : {receipt.steps}")
    print(f"Final drift    : {receipt.final_drift:.4f}")
    print(f"Halt reason    : {receipt.halt_reason}")

    print("\nDrift trajectory:")
    for i, d in enumerate(receipt.drift_trajectory):
        bar = "█" * int(d * 20)
        print(f"  Step {i:02d}: {d:.4f}  {bar}")

    print("\nAgent reputation weights (lower = more drift accumulated):")
    for agent in vault.list_agent_weights():
        print(
            f"  {agent['agent_id']:20s}  "
            f"R_i={agent['drift_integral']:.4f}  "
            f"w_i={agent['weight']:.4f}  "
            f"steps={agent['step_count']}"
        )

    print("\nKitaev penalty tier reference:")
    print("  ValueError / TypeError   → MEDIUM penalty (~0.3)")
    print("  RuntimeError / Exception → HIGH   penalty (~0.6)")
    print("  Success                  → ZERO   penalty (0.0)")
    print("\nDone.")


if __name__ == "__main__":
    main()
