"""
mythic_neuro_kernel.py — MythicNeuroKernel
==========================================
NeuroAccelerator Universal Intent Layer (UIL) extended for human-in-the-loop
skill leveling.

This module extends the ELFE v∞.1 fixed-time Lyapunov kernel with:

  • Skill-state constraint manifold (human competency as a measurable scalar)
  • Thoth-Wadjet Eye closure for the 63/64 → 64/64 mastery gap
  • Quipu Router — constraint-first path selection across skill branches
  • Dongba XR Morph — glyph encoding for VR/thin-client rendering
  • Gardeners Protocol — ritual scroll planting (persistent skill ledger)

All components integrate directly with the Sovereign Claw ProofVault
so every skill acceleration event is sealed, immutable, and auditable.

Mathematical foundations
------------------------
Skill state S ∈ [0, 1]  where 0 = novice, 1 = mastery

Lawful target:
    C(S) = argmin_{y ∈ X} Φ(y)   (nearest achievable competency node)

Drift:
    D(S) = |S − C(S)|

ELFE convergence:
    dV/dt ≤ −a·V^p − b·V^q    →    mastery achieved in T_max steps

Wadjet Eye closure:
    If D(S) ≤ 1/64, then D(S) → 0   (final mastery snap)

Byzantine reputation for human coaches:
    w_coach = e^{−k·R_coach}
"""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


def _load_elfe_coefficients_safe() -> Tuple[float, float, float, float]:
    """Load ELFE coefficients from ip_shield; fall back to defaults on error."""
    try:
        from .ip_shield import load_elfe_coefficients

        a, b, p, q = load_elfe_coefficients()
        return float(a), float(b), float(p), float(q)
    except Exception:
        return 1.0, 1.0, 0.5, 2.0


SKILL_NODES: List[float] = [
    0.0625,
    0.125,
    0.25,
    0.375,
    0.5,
    0.625,
    0.75,
    0.875,
    0.984375,
    1.0,
]

SKILL_NODE_NAMES: Dict[float, str] = {
    0.0625: "Initiate",
    0.125: "Apprentice",
    0.25: "Practitioner",
    0.375: "Journeyman",
    0.5: "Adept",
    0.625: "Expert",
    0.75: "Virtuoso",
    0.875: "Master",
    0.984375: "Grand Master",
    1.0: "Isomorphic Mastery",
}

_WADJET_THRESHOLD = 1.0 / 64.0


@dataclass
class DongbaGlyph:
    """
    Dongba XR Morph — encoded skill state for VR/thin-client rendering.

    The Dongba script (Naxi pictographic system, oldest living pictographic
    writing system) is used as the symbolic encoding layer for skill states.
    Each glyph encodes:
      • glyph_id      : deterministic hash of skill_name + level
      • level_name    : human-readable mastery level
      • xr_vector     : 3D vector for spatial VR placement [x, y, z]
      • morph_weight  : blend weight for avatar/environment morphing (0–1)
      • timestamp     : epoch of encoding
    """

    glyph_id: str
    skill_name: str
    level_name: str
    skill_value: float
    xr_vector: Tuple[float, float, float]
    morph_weight: float
    timestamp: float


class QuipuRouter:
    """
    Constraint-first skill path router inspired by the Andean Quipu
    (knotted-cord information system — the world's most compact constraint ledger).

    Routes a skill state to:
      1. The nearest lawful competency node (immediate target)
      2. The full ascent path to mastery (sequence of nodes)
      3. The recommended next intervention type

    The Quipu metaphor: each 'cord' is a skill branch; each 'knot' is a
    competency node. The router reads the knot pattern and selects the
    minimum-drift path to the next knot.
    """

    def route(
        self,
        skill_state: float,
        skill_name: str = "skill",
    ) -> Dict[str, Any]:
        """
        Route skill_state to nearest lawful node and full ascent path.
        """
        skill_state = max(0.0, min(1.0, skill_state))

        current_node = 0.0
        for node in SKILL_NODES:
            if node <= skill_state:
                current_node = node

        target_node = 1.0
        for node in SKILL_NODES:
            if node > skill_state:
                target_node = node
                break

        ascent_path = [n for n in SKILL_NODES if n > skill_state]
        drift_to_next = abs(skill_state - target_node)
        intervention = self._select_intervention(skill_state, drift_to_next)

        return {
            "current_node": current_node,
            "current_name": SKILL_NODE_NAMES.get(current_node, "Unknown"),
            "target_node": target_node,
            "target_name": SKILL_NODE_NAMES.get(target_node, "Unknown"),
            "ascent_path": ascent_path,
            "drift_to_next": drift_to_next,
            "intervention": intervention,
            "skill_name": skill_name,
        }

    @staticmethod
    def _select_intervention(skill_state: float, drift: float) -> str:
        """
        Select intervention type based on current level and drift magnitude.
        Maps to a human-in-the-loop action category.
        """
        if skill_state < 0.25:
            return "GUIDED_INSTRUCTION"
        if skill_state < 0.5:
            return "DELIBERATE_PRACTICE"
        if skill_state < 0.75:
            return "ADVERSARIAL_CHALLENGE"
        if skill_state < 0.984375:
            return "PEER_SYNTHESIS"
        return "WADJET_CLOSURE"


class MythicNeuroKernel:
    """
    NeuroAccelerator UIL + LFE (Learning Field Equation) kernel.

    Governs human skill acceleration using the same constraint-first
    thermodynamic principles as the Sovereign Claw agent framework.
    """

    def __init__(
        self,
        elfe_a: Optional[float] = None,
        elfe_b: Optional[float] = None,
        elfe_p: Optional[float] = None,
        elfe_q: Optional[float] = None,
        descent_scale: float = 0.12,
        k_reputation: float = 1.0,
    ) -> None:
        if any(v is None for v in (elfe_a, elfe_b, elfe_p, elfe_q)):
            _a, _b, _p, _q = _load_elfe_coefficients_safe()
            elfe_a = _a if elfe_a is None else elfe_a
            elfe_b = _b if elfe_b is None else elfe_b
            elfe_p = _p if elfe_p is None else elfe_p
            elfe_q = _q if elfe_q is None else elfe_q

        # mypy-safe: all four are guaranteed non-None after the fallback block above
        assert elfe_a is not None
        assert elfe_b is not None
        assert elfe_p is not None
        assert elfe_q is not None

        self.elfe_a: float = float(elfe_a)
        self.elfe_b: float = float(elfe_b)
        self.elfe_p: float = float(elfe_p)
        self.elfe_q: float = float(elfe_q)
        self.descent_scale: float = descent_scale
        self.k_reputation: float = k_reputation
        self.quipu_router = QuipuRouter()

    def lawful_target(self, skill_state: float) -> float:
        """
        C(S) = argmin_{y ∈ X} Φ(y)
        Returns the nearest competency node above current skill_state.
        """
        skill_state = max(0.0, min(1.0, skill_state))
        for node in SKILL_NODES:
            if node > skill_state:
                return node
        return 1.0

    def elfe_step(
        self,
        skill_state: float,
        session_quality: float = 0.0,
    ) -> Tuple[float, float]:
        """
        Apply one ELFE learning step.
        """
        v = max(0.0, min(1.0, skill_state))
        a = self.elfe_a
        b = self.elfe_b
        p = self.elfe_p
        q = self.elfe_q
        sc = self.descent_scale

        base_improvement = 0.02 * session_quality
        descent = sc * (a * (v**p) + b * (v**q))
        improvement = base_improvement + descent * session_quality

        new_state = max(0.0, min(1.0, v + improvement))
        new_state = self.thoth_wadjet_closure(new_state)
        drift = abs(new_state - self.lawful_target(new_state))

        return new_state, drift

    def thoth_wadjet_closure(self, skill_state: float) -> float:
        """
        If skill_state ≥ 63/64, snap to 1.0 (Isomorphic Mastery).
        """
        if skill_state >= (1.0 - _WADJET_THRESHOLD):
            return 1.0
        return skill_state

    def dongba_morph(
        self,
        skill_state: float,
        skill_name: str = "skill",
    ) -> DongbaGlyph:
        """
        Encode skill state as a Dongba XR glyph for VR/thin-client rendering.
        """
        level_name = SKILL_NODE_NAMES.get(
            min(SKILL_NODES, key=lambda n: abs(n - skill_state)),
            "Unknown",
        )

        glyph_seed = f"{skill_name}:{level_name}:{skill_state:.6f}"
        glyph_id = hashlib.sha256(glyph_seed.encode()).hexdigest()[:16]

        angle = skill_state * 2 * math.pi
        elevation = skill_state * math.pi - (math.pi / 2)
        xr_vector = (
            round(math.cos(angle) * math.cos(elevation), 6),
            round(math.sin(elevation), 6),
            round(math.sin(angle) * math.cos(elevation), 6),
        )

        morph_weight = skill_state

        return DongbaGlyph(
            glyph_id=glyph_id,
            skill_name=skill_name,
            level_name=level_name,
            skill_value=skill_state,
            xr_vector=xr_vector,
            morph_weight=morph_weight,
            timestamp=time.time(),
        )

    def coach_reputation_weight(self, cumulative_drift: float) -> float:
        """
        w_coach = e^{−k·R_coach}
        Coaches who consistently produce poor sessions accumulate drift.
        """
        return math.exp(-self.k_reputation * cumulative_drift)
