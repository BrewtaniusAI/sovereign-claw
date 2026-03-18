"""
weavers_kernel.py — WeaversKernel
==================================
Human-in-the-loop skill leveling system integrating:

  • MythicNeuroKernel   — NeuroAccelerator UIL + ELFE LFE kernel
  • GardenersProtocol   — Ritual scroll planting + bloom/wilt lifecycle
  • QuipuRouter         — Constraint-first skill path selection
  • DongbaXR Morph      — VR glyph encoding for thin-client rendering
  • ProofVault          — WORM audit seal on every acceleration event
  • LaneRouter          — Tri-temporal governance (Reflex/Deliberate/Auth)

The WeaversKernel is the single entry point for all human skill
acceleration events.  It governs the complete loop:

    Human session → ELFE step → drift update → Quipu routing →
    Dongba glyph → Gardeners scroll → ProofVault seal → Receipt

Human-in-the-loop (HITL) governance
-------------------------------------
Every acceleration event requires two human inputs:
  1. coach_quality   (0.0–1.0) — coach's assessment of session quality
  2. learner_quality (0.0–1.0) — learner's self-assessment

These are Bayesian-weighted to produce a single session quality score
that feeds the ELFE kernel.  Disagreement > 0.3 between coach and
learner automatically routes to PEER_SYNTHESIS intervention.

The HITL loop enforces the no-skip invariant from LaneRouter:
  Lane 1 (REFLEX)       — input validation + glyph encoding
  Lane 2 (DELIBERATE)   — coach + learner quality negotiation
  Lane 3 (AUTHORITATIVE)— ProofVault seal + scroll bloom check

IP Protection
-------------
All exported classes expose only the public interface documented here.
Internal mathematical symbols, proprietary GOD FILE coefficients, and
routing logic are encapsulated.  The accelerate_skill() method is the
only externally callable surface.  There is no public accessor for the
raw ELFE kernel state, scroll database path, or ProofVault trace IDs
beyond what is returned in the AccelerationReceipt.

© Brewtanius Ink LLC / Immortal Tek Inc.  All rights reserved.
CollectiveOS IP.  Community Edition: Apache-2.0 for the interface layer.
Proprietary: MythicNeuroKernel internals, GOD FILE v∞.1 coefficients.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .gardeners_protocol import GardenersProtocol
from .ip_shield import seal_with_build_fingerprint
from .lanes import LaneRouter
from .mythic_neuro_kernel import MythicNeuroKernel
from .proof_vault import ProofVault, StepRecord


@dataclass
class AccelerationReceipt:
    """
    Sealed output of a single WeaversKernel acceleration event.

    Fields
    ------
    learner_id         : Anonymised learner identifier
    skill_name         : Name of the skill being leveled
    skill_before       : Competency value before session
    skill_after        : Competency value after session
    drift              : Remaining drift to next lawful node
    target_node        : Immediate target competency node
    target_name        : Human-readable name of target node
    intervention_next  : Recommended next intervention type
    glyph_id           : Dongba XR glyph hash for VR rendering
    scroll_id          : Gardeners Protocol scroll identifier
    vault_trace_id     : ProofVault WORM trace identifier
    session_id         : Individual session record ID
    lane_status        : Final LaneRouter status
    bloomed            : True if target node reached this session
    timestamp          : Epoch of sealing
    """

    learner_id: str
    skill_name: str
    skill_before: float
    skill_after: float
    drift: float
    target_node: float
    target_name: str
    intervention_next: str
    glyph_id: str
    scroll_id: str
    vault_trace_id: str
    session_id: str
    lane_status: str
    bloomed: bool
    timestamp: float = field(default_factory=time.time)


class WeaversKernel:
    """
    Human-in-the-loop skill leveling orchestrator.

    Parameters
    ----------
    vault          : ProofVault instance (shared with agent system if desired)
    gardeners_db   : Path override for Gardeners Protocol database
    neuro_config   : Dict of MythicNeuroKernel constructor kwargs
    coach_weight   : Default Bayesian weight for coach quality (0.0–1.0)
    """

    def __init__(
        self,
        vault: Optional[ProofVault] = None,
        gardeners_db: Optional[Path] = None,
        neuro_config: Optional[Dict[str, Any]] = None,
        coach_weight: float = 0.6,
    ) -> None:
        self._neuro = MythicNeuroKernel(**(neuro_config or {}))
        self._gardeners = GardenersProtocol(
            db_path=gardeners_db or GardenersProtocol.__init__.__defaults__[0]
            if not gardeners_db
            else gardeners_db
        )
        self._vault = vault or ProofVault()
        self._coach_weight = coach_weight

    def accelerate_skill(
        self,
        skill_state: float,
        coach_quality: float,
        learner_quality: float,
        skill_name: str = "skill",
        learner_id: str = "default_learner",
        coach_id: Optional[str] = None,
        scroll_id: Optional[str] = None,
        notes: str = "",
        coach_weight: Optional[float] = None,
    ) -> AccelerationReceipt:
        """
        Execute one complete skill acceleration cycle with HITL governance.

        Parameters
        ----------
        skill_state     : Current competency S ∈ [0, 1]
        coach_quality   : Coach's session quality rating ∈ [0, 1]
        learner_quality : Learner's self-assessment ∈ [0, 1]
        skill_name      : Name of the skill being leveled
        learner_id      : Anonymised learner identifier
        coach_id        : Optional coach identifier (for reputation tracking)
        scroll_id       : Existing scroll to continue, or None to plant new
        notes           : Optional session notes (stored in Gardeners ledger)
        coach_weight    : Override default coach Bayesian weight

        Returns
        -------
        AccelerationReceipt — sealed, immutable record of the event
        """
        skill_state = max(0.0, min(1.0, skill_state))
        coach_quality = max(0.0, min(1.0, coach_quality))
        learner_quality = max(0.0, min(1.0, learner_quality))
        cw = coach_weight if coach_weight is not None else self._coach_weight

        lane_router = LaneRouter(max_deliberate_loops=1)

        _ = self._neuro.dongba_morph(skill_state, skill_name)

        lane_router.advance(approved=False, drift=skill_state)

        disagreement = abs(coach_quality - learner_quality)
        if disagreement > 0.3:
            intervention_override = "PEER_SYNTHESIS"
        else:
            intervention_override = None

        session_quality = cw * coach_quality + (1.0 - cw) * learner_quality

        new_skill_state, drift = self._neuro.elfe_step(skill_state, session_quality)

        route = self._neuro.quipu_router.route(new_skill_state, skill_name)
        intervention = intervention_override or route["intervention"]
        target_node = route["target_node"]
        target_name = route["target_name"]

        new_glyph = self._neuro.dongba_morph(new_skill_state, skill_name)

        if scroll_id is None:
            scroll_id = self._gardeners.plant_skill(
                skill_state=skill_state,
                glyph_id=new_glyph.glyph_id,
                learner_id=learner_id,
                skill_name=skill_name,
                target_node=target_node,
                target_name=target_name,
            )

        session_rec = self._gardeners.record_session(
            scroll_id=scroll_id,
            skill_before=skill_state,
            skill_after=new_skill_state,
            coach_quality=coach_quality,
            learner_quality=learner_quality,
            intervention_type=intervention,
            drift_after=drift,
            coach_id=coach_id,
            notes=notes,
            coach_weight=cw,
        )

        bloomed = new_skill_state >= target_node

        trace_meta = seal_with_build_fingerprint(
            {
                "skill_name": skill_name,
                "learner_id": learner_id,
                "target_node": target_node,
                "target_name": target_name,
                "scroll_id": scroll_id,
                "disagreement": round(disagreement, 4),
            }
        )
        vault_trace_id = self._vault.create_trace(
            objective=f"skill_acceleration:{skill_name}:{learner_id}",
            meta=trace_meta,
        )

        self._vault.append_step(
            StepRecord(
                trace_id=vault_trace_id,
                step_index=0,
                timestamp=time.time(),
                node="weavers_kernel",
                action="SKILL_ACCELERATION",
                drift=drift,
                status="ISOMORPHIC_CLOSURE" if bloomed else "CONTINUE_DESCENT",
                payload={
                    "skill_before": skill_state,
                    "skill_after": new_skill_state,
                    "drift": drift,
                    "session_quality": round(session_quality, 4),
                    "disagreement": round(disagreement, 4),
                    "intervention": intervention,
                    "glyph_id": new_glyph.glyph_id,
                    "xr_vector": new_glyph.xr_vector,
                    "morph_weight": new_glyph.morph_weight,
                    "scroll_id": scroll_id,
                    "bloomed": bloomed,
                },
            )
        )

        if coach_id:
            self._vault.update_agent_reputation(
                agent_id=f"coach:{coach_id}",
                step_drift=drift * (1.0 - coach_quality),
            )

        lane_router.advance(approved=True, drift=drift)

        return AccelerationReceipt(
            learner_id=learner_id,
            skill_name=skill_name,
            skill_before=skill_state,
            skill_after=new_skill_state,
            drift=drift,
            target_node=target_node,
            target_name=target_name,
            intervention_next=intervention,
            glyph_id=new_glyph.glyph_id,
            scroll_id=scroll_id,
            vault_trace_id=vault_trace_id,
            session_id=session_rec.session_id,
            lane_status=lane_router.final_status or "CONTINUE_DESCENT",
            bloomed=bloomed,
        )

    def get_learner_progress(self, learner_id: str) -> List[Dict[str, Any]]:
        """Return all scrolls and bloom status for a learner."""
        return self._gardeners.get_learner_progress(learner_id)

    def get_coach_weight(self, coach_id: str, k: float = 1.0) -> float:
        """Return the current Byzantine reputation weight for a coach."""
        return self._vault.get_agent_reputation_weight(f"coach:{coach_id}", k=k)

    def run_garden_maintenance(self) -> List[str]:
        """Check for wilted scrolls. Returns list of wilted scroll_ids."""
        return self._gardeners.run_wilt_check()