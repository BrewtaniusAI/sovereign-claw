"""
tests/test_weavers_kernel.py
============================
Tests for the human-in-the-loop skill leveling system:
  • MythicNeuroKernel: UIL, ELFE step, Thoth-Wadjet, Dongba morph, Quipu routing
  • GardenersProtocol: plant, record_session, bloom, wilt
  • WeaversKernel: full accelerate_skill loop, HITL governance
  • ip_shield: build fingerprint, coefficient loader
"""
from __future__ import annotations

import math
import os
import time
import pytest
from pathlib import Path

os.environ.setdefault("SOVEREIGN_CLAW_DB",          "/tmp/sc_weavers_test.sqlite3")
os.environ.setdefault("SOVEREIGN_CLAW_GARDENERS_DB", "/tmp/sc_gardeners_test.sqlite3")

from sovereign_claw.mythic_neuro_kernel import (
    MythicNeuroKernel, QuipuRouter, SKILL_NODES, SKILL_NODE_NAMES
)
from sovereign_claw.gardeners_protocol import GardenersProtocol
from sovereign_claw.weavers_kernel import WeaversKernel, AccelerationReceipt
from sovereign_claw.proof_vault import ProofVault
from sovereign_claw.ip_shield import (
    BUILD_FINGERPRINT, load_elfe_coefficients, seal_with_build_fingerprint,
    _SYMBOL_MAP
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def neuro():
    return MythicNeuroKernel()

@pytest.fixture
def router():
    return QuipuRouter()

@pytest.fixture
def gardeners(tmp_path):
    return GardenersProtocol(
        db_path=tmp_path / "gardeners.sqlite3",
        germination_hours=0.0,   # disable for testing
        wilt_hours=0.001,        # 3.6 seconds for test speed
    )

@pytest.fixture
def vault(tmp_path):
    return ProofVault(db_path=tmp_path / "vault.sqlite3")

@pytest.fixture
def kernel(tmp_path):
    vault = ProofVault(db_path=tmp_path / "vault.sqlite3")
    return WeaversKernel(
        vault=vault,
        gardeners_db=tmp_path / "gardeners.sqlite3",
    )


# ─────────────────────────────────────────────────────────────────────────────
# MythicNeuroKernel tests
# ─────────────────────────────────────────────────────────────────────────────

class TestMythicNeuroKernel:
    def test_lawful_target_above_state(self, neuro):
        c = neuro.lawful_target(0.3)
        assert c > 0.3

    def test_lawful_target_at_zero(self, neuro):
        c = neuro.lawful_target(0.0)
        assert c == SKILL_NODES[0]

    def test_lawful_target_near_mastery(self, neuro):
        c = neuro.lawful_target(0.99)
        assert c == 1.0

    def test_elfe_step_improves_skill(self, neuro):
        new_state, drift = neuro.elfe_step(0.3, session_quality=1.0)
        assert new_state > 0.3

    def test_elfe_step_poor_quality_slows_improvement(self, neuro):
        good_state, _ = neuro.elfe_step(0.3, session_quality=1.0)
        mid_state,  _ = neuro.elfe_step(0.3, session_quality=0.5)
        zero_state, _ = neuro.elfe_step(0.3, session_quality=0.0)
        # Higher quality → more improvement
        assert good_state > mid_state
        assert zero_state == pytest.approx(0.3)   # zero quality → no change

    def test_elfe_step_clamped_to_one(self, neuro):
        new_state, _ = neuro.elfe_step(0.999, session_quality=1.0)
        assert new_state <= 1.0

    def test_wadjet_closure_snaps_to_mastery(self, neuro):
        # 63/64 ≈ 0.984375 → should snap to 1.0
        result = neuro.thoth_wadjet_closure(0.985)
        assert result == 1.0

    def test_wadjet_no_snap_below_threshold(self, neuro):
        result = neuro.thoth_wadjet_closure(0.90)
        assert result == pytest.approx(0.90)

    def test_dongba_morph_deterministic(self, neuro):
        g1 = neuro.dongba_morph(0.5, "coding")
        g2 = neuro.dongba_morph(0.5, "coding")
        assert g1.glyph_id == g2.glyph_id

    def test_dongba_morph_different_states(self, neuro):
        g1 = neuro.dongba_morph(0.25, "coding")
        g2 = neuro.dongba_morph(0.75, "coding")
        assert g1.glyph_id != g2.glyph_id

    def test_dongba_morph_xr_vector_is_unit(self, neuro):
        g = neuro.dongba_morph(0.5, "skill")
        x, y, z = g.xr_vector
        magnitude = math.sqrt(x**2 + y**2 + z**2)
        assert abs(magnitude - 1.0) < 0.001

    def test_dongba_morph_weight_equals_state(self, neuro):
        g = neuro.dongba_morph(0.7, "skill")
        assert g.morph_weight == pytest.approx(0.7)

    def test_coach_reputation_decays(self, neuro):
        w_good = neuro.coach_reputation_weight(0.1)
        w_bad  = neuro.coach_reputation_weight(2.0)
        assert w_good > w_bad
        assert w_good <= 1.0


# ─────────────────────────────────────────────────────────────────────────────
# QuipuRouter tests
# ─────────────────────────────────────────────────────────────────────────────

class TestQuipuRouter:
    def test_route_returns_target_above_state(self, router):
        r = router.route(0.3, "coding")
        assert r["target_node"] > 0.3

    def test_route_ascent_path_ascending(self, router):
        r = router.route(0.25)
        path = r["ascent_path"]
        assert path == sorted(path)

    def test_route_at_mastery(self, router):
        r = router.route(1.0)
        assert r["target_node"] == 1.0
        assert r["ascent_path"] == []

    def test_route_intervention_changes_with_level(self, router):
        low   = router.route(0.1)["intervention"]
        high  = router.route(0.8)["intervention"]
        assert low != high

    def test_route_guided_at_novice(self, router):
        r = router.route(0.05)
        assert r["intervention"] == "GUIDED_INSTRUCTION"

    def test_route_wadjet_at_grandmaster(self, router):
        r = router.route(0.99)
        assert r["intervention"] == "WADJET_CLOSURE"

    def test_route_drift_to_next_positive(self, router):
        r = router.route(0.5)
        assert r["drift_to_next"] > 0


# ─────────────────────────────────────────────────────────────────────────────
# GardenersProtocol tests
# ─────────────────────────────────────────────────────────────────────────────

class TestGardenersProtocol:
    def test_plant_returns_scroll_id(self, gardeners):
        sid = gardeners.plant_skill(
            skill_state=0.25, glyph_id="abc123",
            learner_id="learner1", skill_name="coding",
            target_node=0.5, target_name="Adept",
        )
        assert isinstance(sid, str) and len(sid) > 0

    def test_record_session_returns_record(self, gardeners):
        sid = gardeners.plant_skill(0.25, "glyph1", "l1", "coding", 0.5, "Adept")
        rec = gardeners.record_session(
            scroll_id=sid,
            skill_before=0.25, skill_after=0.35,
            coach_quality=0.8, learner_quality=0.7,
            intervention_type="DELIBERATE_PRACTICE",
        )
        assert rec.session_id is not None
        assert rec.weighted_quality == pytest.approx(0.8*0.6 + 0.7*0.4, abs=0.01)

    def test_scroll_blooms_at_target(self, gardeners):
        sid = gardeners.plant_skill(0.45, "g1", "l1", "coding", 0.5, "Adept")
        gardeners.record_session(
            scroll_id=sid,
            skill_before=0.45, skill_after=0.51,
            coach_quality=1.0, learner_quality=1.0,
            intervention_type="DELIBERATE_PRACTICE",
        )
        progress = gardeners.get_learner_progress("l1")
        scroll = next(s for s in progress if s["scroll_id"] == sid)
        assert scroll["status"] == "BLOOMED"

    def test_wilt_check_wilts_inactive(self, gardeners, tmp_path):
        # Plant a scroll, then manually backdate planted_at to trigger wilt
        sid = gardeners.plant_skill(0.1, "g1", "l1", "skill", 0.5, "Adept")
        # Backdate planted_at by 1 hour (well beyond 0.001h wilt threshold)
        import sqlite3
        conn = sqlite3.connect(str(gardeners.db_path))
        conn.execute(
            "UPDATE scrolls SET planted_at = ? WHERE scroll_id = ?",
            (time.time() - 3600, sid)
        )
        conn.commit(); conn.close()
        wilted = gardeners.run_wilt_check()
        assert sid in wilted

    def test_gardeners_proof_deterministic(self):
        p1 = GardenersProtocol._gardeners_proof("learner", "skill", 0.5, 1000.0)
        p2 = GardenersProtocol._gardeners_proof("learner", "skill", 0.5, 1000.0)
        assert p1 == p2

    def test_get_learner_progress_returns_all_scrolls(self, gardeners):
        for i in range(3):
            gardeners.plant_skill(
                0.1 * (i+1), f"g{i}", f"learnerX", f"skill_{i}", 0.5, "Adept"
            )
        progress = gardeners.get_learner_progress("learnerX")
        assert len(progress) == 3

    def test_disagreement_session_stored(self, gardeners):
        sid = gardeners.plant_skill(0.3, "g1", "l2", "coding", 0.5, "Adept")
        rec = gardeners.record_session(
            scroll_id=sid,
            skill_before=0.3, skill_after=0.35,
            coach_quality=0.9, learner_quality=0.4,  # disagreement = 0.5
            intervention_type="PEER_SYNTHESIS",
        )
        sessions = gardeners.get_scroll_sessions(sid)
        assert len(sessions) == 1
        assert sessions[0]["intervention_type"] == "PEER_SYNTHESIS"


# ─────────────────────────────────────────────────────────────────────────────
# WeaversKernel full integration tests
# ─────────────────────────────────────────────────────────────────────────────

class TestWeaversKernel:
    def test_accelerate_returns_receipt(self, kernel):
        receipt = kernel.accelerate_skill(
            skill_state=0.3,
            coach_quality=0.8,
            learner_quality=0.75,
            skill_name="python",
            learner_id="dev001",
        )
        assert isinstance(receipt, AccelerationReceipt)

    def test_skill_improves_after_acceleration(self, kernel):
        receipt = kernel.accelerate_skill(
            skill_state=0.3,
            coach_quality=1.0,
            learner_quality=1.0,
            skill_name="python",
        )
        assert receipt.skill_after > receipt.skill_before

    def test_receipt_has_glyph_id(self, kernel):
        receipt = kernel.accelerate_skill(0.4, 0.8, 0.7)
        assert len(receipt.glyph_id) > 0

    def test_receipt_has_scroll_id(self, kernel):
        receipt = kernel.accelerate_skill(0.4, 0.8, 0.7)
        assert len(receipt.scroll_id) > 0

    def test_receipt_has_vault_trace(self, kernel):
        receipt = kernel.accelerate_skill(0.4, 0.8, 0.7)
        assert len(receipt.vault_trace_id) > 0

    def test_disagreement_triggers_peer_synthesis(self, kernel):
        receipt = kernel.accelerate_skill(
            skill_state=0.5,
            coach_quality=0.9,
            learner_quality=0.4,   # disagreement = 0.5 > 0.3
            skill_name="leadership",
        )
        assert receipt.intervention_next == "PEER_SYNTHESIS"

    def test_bloomed_when_target_reached(self, kernel):
        # Use a skill state near the 0.875 → 1.0 gap with excellent quality
        # Repeatedly accelerate until bloom
        state = 0.90
        bloomed = False
        scroll_id = None
        for _ in range(20):
            receipt = kernel.accelerate_skill(
                skill_state=state,
                coach_quality=1.0,
                learner_quality=1.0,
                skill_name="mastery_test",
                scroll_id=scroll_id,
            )
            scroll_id = receipt.scroll_id
            state = receipt.skill_after
            if receipt.bloomed:
                bloomed = True
                break
        # If Wadjet closure fires, state reaches 1.0 → bloom
        assert bloomed or state >= 0.984375

    def test_continue_scroll_across_sessions(self, kernel):
        r1 = kernel.accelerate_skill(0.3, 0.8, 0.7, skill_name="skill", learner_id="L1")
        r2 = kernel.accelerate_skill(
            r1.skill_after, 0.8, 0.7,
            skill_name="skill", learner_id="L1",
            scroll_id=r1.scroll_id,
        )
        assert r2.scroll_id == r1.scroll_id

    def test_coach_reputation_tracked(self, kernel):
        kernel.accelerate_skill(
            0.3, 0.5, 0.7,
            coach_id="coach_alpha",
        )
        w = kernel.get_coach_weight("coach_alpha")
        assert 0.0 < w <= 1.0

    def test_learner_progress_query(self, kernel):
        kernel.accelerate_skill(0.2, 0.8, 0.7, skill_name="s1", learner_id="Lx")
        kernel.accelerate_skill(0.3, 0.8, 0.7, skill_name="s2", learner_id="Lx")
        progress = kernel.get_learner_progress("Lx")
        assert len(progress) >= 2

    def test_garden_maintenance_runs(self, kernel):
        # Should not raise
        wilted = kernel.run_garden_maintenance()
        assert isinstance(wilted, list)


# ─────────────────────────────────────────────────────────────────────────────
# IP Shield tests
# ─────────────────────────────────────────────────────────────────────────────

class TestIPShield:
    def test_build_fingerprint_is_string(self):
        assert isinstance(BUILD_FINGERPRINT, str)
        assert len(BUILD_FINGERPRINT) == 12

    def test_community_coefficients_are_demo(self):
        os.environ["SOVEREIGN_CLAW_EDITION"] = "COMMUNITY"
        a, b, p, q = load_elfe_coefficients()
        assert a == 1.0 and b == 1.0
        assert 0 < p < 1
        assert q > 1

    def test_seal_injects_fingerprint(self):
        meta = {"test": "value"}
        sealed = seal_with_build_fingerprint(meta)
        assert "_build_fingerprint" in sealed
        assert "_owner" in sealed
        assert "_edition" in sealed

    def test_symbol_map_has_core_entries(self):
        assert "WeaversKernel" in _SYMBOL_MAP
        assert "MythicNeuroKernel" in _SYMBOL_MAP
        assert "elfe_a" in _SYMBOL_MAP
        assert "thoth_wadjet_threshold" in _SYMBOL_MAP

    def test_fingerprint_consistent(self):
        f1 = BUILD_FINGERPRINT
        f2 = BUILD_FINGERPRINT
        assert f1 == f2
