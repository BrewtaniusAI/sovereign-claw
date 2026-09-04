"""Issue #17 regression: legacy skill acceleration cannot mint production closure."""

from __future__ import annotations

from pathlib import Path

from sovereign_claw.proof_vault import ProofVault
from sovereign_claw.weavers_kernel import WeaversKernel


def test_bloomed_skill_is_unverified_and_non_authorizing(tmp_path: Path, monkeypatch) -> None:
    vault = ProofVault(db_path=tmp_path / "vault.sqlite3")
    kernel = WeaversKernel(
        vault=vault,
        gardeners_db=tmp_path / "gardeners.sqlite3",
    )
    persisted = []

    def capture(record):
        persisted.append(record)
        return None

    monkeypatch.setattr(vault, "append_step", capture)
    receipt = kernel.accelerate_skill(
        skill_state=0.999,
        coach_quality=1.0,
        learner_quality=1.0,
        skill_name="closure-boundary",
        learner_id="learner:closure-boundary",
    )

    assert receipt.bloomed is True
    assert receipt.lane_status == "UNVERIFIED_CONVERGENCE"
    assert receipt.lane_status != "ISOMORPHIC_CLOSURE"
    assert persisted
    assert persisted[-1].status == "UNVERIFIED_CONVERGENCE"
    assert persisted[-1].payload["closure_authority"] is False
