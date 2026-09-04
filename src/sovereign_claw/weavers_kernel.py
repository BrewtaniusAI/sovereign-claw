from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from sovereign_claw.gardeners_protocol import GardenersProtocol
from sovereign_claw.ip_shield import seal_with_build_fingerprint
from sovereign_claw.mythic_neuro_kernel import MythicNeuroKernel
from sovereign_claw.proof_vault import ProofVault, StepRecord


@dataclass
class AccelerationReceipt:
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
    def __init__(
        self,
        vault: Optional[ProofVault] = None,
        gardeners_db: Optional[Path] = None,
        neuro_config: Optional[Dict[str, Any]] = None,
        coach_weight: float = 0.6,
    ) -> None:
        self._neuro = MythicNeuroKernel(**(neuro_config or {}))

        if gardeners_db is not None:
            db_path: Path = gardeners_db
        else:
            defaults = GardenersProtocol.__init__.__defaults__
            if defaults and len(defaults) > 0 and defaults[0] is not None:
                db_path = cast(Path, defaults[0])
            else:
                db_path = Path("gardeners.sqlite3")

        self._gardeners = GardenersProtocol(db_path=db_path)
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
        """Advance the learning model without asserting production closure authority.

        This legacy skill-acceleration surface uses MythicNeuroKernel's synthetic skill
        dynamics rather than the issue #17 measured ConstraintEvaluator/ClosureDecision
        pipeline.  A reached skill target may therefore be reported only as
        ``UNVERIFIED_CONVERGENCE``.  It must never mint ``ISOMORPHIC_CLOSURE`` or an
        authoritative lane transition.
        """
        skill_state = max(0.0, min(1.0, skill_state))
        coach_quality = max(0.0, min(1.0, coach_quality))
        learner_quality = max(0.0, min(1.0, learner_quality))
        cw = coach_weight if coach_weight is not None else self._coach_weight

        self._neuro.dongba_morph(skill_state, skill_name)

        disagreement = abs(coach_quality - learner_quality)
        intervention_override = "PEER_SYNTHESIS" if disagreement > 0.3 else None

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
        convergence_status = "UNVERIFIED_CONVERGENCE" if bloomed else "CONTINUE_DESCENT"

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
                status=convergence_status,
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
                    "closure_authority": False,
                },
            )
        )

        if coach_id:
            self._vault.update_agent_reputation(
                agent_id=f"coach:{coach_id}",
                step_drift=drift * (1.0 - coach_quality),
            )

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
            lane_status=convergence_status,
            bloomed=bloomed,
        )

    def get_learner_progress(self, learner_id: str) -> List[Dict[str, Any]]:
        return self._gardeners.get_learner_progress(learner_id)

    def get_coach_weight(self, coach_id: str, k: float = 1.0) -> float:
        return self._vault.get_agent_reputation_weight(f"coach:{coach_id}", k=k)

    def run_garden_maintenance(self) -> List[str]:
        return self._gardeners.run_wilt_check()
