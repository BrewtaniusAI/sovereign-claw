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

os.environ.setdefault("SOVEREIGN_CLAW_DB", "/tmp/sc_weavers_test.sqlite3")
os.environ.setdefault("SOVEREIGN_CLAW_GARDENERS_DB", "/tmp/sc_gardeners_test.sqlite3")

from sovereign_claw.mythic_neuro_kernel import MythicNeuroKernel, QuipuRouter, SKILL_NODES
from sovereign_claw.gardeners_protocol import GardenersProtocol
from sovereign_claw.weavers_kernel import WeaversKernel
from sovereign_claw.proof_vault import ProofVault


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
        germination_hours=0.0,
        wilt_hours=0.001,
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
        mid_state, _ = neuro.elfe_step(0.3, session_quality=0.5)
        zero_state, _ = neuro.elfe_step(0.3, session_quality=0.0)
        assert good_state > mid_state
        assert zero_state == pytest.approx(0.3)

    def test_elfe_step_clamped_to_one(self, neuro):
        new_state, _ = neuro.elfe_step(0.999, session_quality=1.0)
        assert new_state <= 1.0

    def test_wadjet_closure_snaps_to_mastery(self, neuro):
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
        w_bad = neuro.coach_reputation_weight(2.0)
        assert w_good > w_bad
        assert w_good <= 1.0


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
        low = router.route(0.1)["intervention"]
        high = router.route(0.8)["intervention"]
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


class TestGardenersProtocol:
    def test_plant_returns_scroll_id(self, gardeners):
        sid = gardeners.plant_skill(
            skill_state=0.25,
            glyph_id="abc123",
            learner_id="learner1",
            skill_name="coding",
            target_node=0.5,
            target_name="Adept",
        )
        assert isinstance(sid, str) and len(sid) > 0

    def test_record_session_returns_record(self, gardeners):
        sid = gardeners.plant_skill(0.25, "glyph1", "l1", "coding", 0.5, "Adept")
        rec = gardeners.record_session(
            scroll_id=sid,
            skill_before=0.25,
            skill_after=0.35,
            coach_quality=0.8,
            learner_quality=0.7,
            intervention_type="DELIBERATE_PRACTICE",
        )
        assert rec.session_id is not None
        assert rec.weighted_quality == pytest.approx(0.8 * 0.6 + 0.7 * 0.4, abs=0.01)

    def test_scroll_blooms_at_target(self, gardeners):
        sid = gardeners.plant_skill(0.45, "g1", "l1", "coding", 0.5, "Adept")
        gardeners.record_session(
            scroll_id=sid,
            skill_before=0.45,
            skill_after=0.51,
            coach_quality=1.0,
            learner_quality=1.0,
            intervention_type="DELIBERATE_PRACTICE",
        )
        progress = gardeners.get_learner_progress("l1")
        scroll = next(s for s in progress if s["scroll_id"] == sid)
        assert scroll["status"] == "BLOOMED"

    def test_wilt_check_wilts_inactive(self, gardeners, tmp_path):
        sid = gardeners.plant_skill(0.1, "g1", "l1", "skill", 0.5, "Adept")
        import sqlite3

        conn = sqlite3.connect(str(gardeners.db_path))
        conn.execute(
            "UPDATE scrolls SET planted_at = ? WHERE scroll_id = ?", (time.time() - 3600, sid)
        )
        conn.commit()
        conn.close()
        wilted = gardeners.run_wilt_check()
        assert sid in wilted
