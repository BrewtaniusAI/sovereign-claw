"""
Adversarial regressions for the issue #15 P0 review findings.

These tests specifically guard the authority gaps found after the first
ProofVault write-time-chain implementation.
"""

from __future__ import annotations

import copy
import sqlite3
import threading
from pathlib import Path

import pytest

from sovereign_claw.proof_vault import (
    HASH_VERSION,
    PROVENANCE_LEGACY,
    PROVENANCE_VERIFIED,
    SCHEMA_VERSION,
    LedgerIntegrityError,
    ProofVault,
    StepRecord,
)
from sovereign_claw.receipts import ReceiptBuilder


def _vault(tmp_path: Path, name: str = "authority.sqlite3") -> ProofVault:
    return ProofVault(db_path=tmp_path / name)


def _step(trace_id: str, index: int) -> StepRecord:
    return StepRecord(
        trace_id=trace_id,
        step_index=index,
        timestamp=1_700_100_000.0 + index,
        node="regression",
        action=f"STEP_{index}",
        drift=max(0.0, 1.0 - index * 0.1),
        status="OK",
        payload={"tool": "echo_text", "index": index},
    )


def _force_meta(db_path: Path, assignment: str, value: object) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(f"UPDATE chain_meta SET {assignment}=? WHERE id=1", (value,))
    conn.commit()
    conn.close()


def _legacy_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE traces(
            trace_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            objective TEXT,
            meta JSON
        );
        CREATE TABLE steps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            step_index INTEGER NOT NULL,
            timestamp REAL NOT NULL,
            node TEXT NOT NULL,
            action TEXT NOT NULL,
            drift REAL NOT NULL,
            status TEXT NOT NULL,
            payload JSON NOT NULL
        );
        INSERT INTO traces
        VALUES ('legacy-trace', 1000.0, 'legacy objective', '{"source":"old"}');
        INSERT INTO steps
        VALUES (
            NULL, 'legacy-trace', 0, 1000.0,
            'legacy-node', 'LEGACY', 0.5, 'OK', '{"v":1}'
        );
        """
    )
    conn.commit()
    conn.close()


def test_create_trace_projection_rolls_back_when_authority_is_broken(
    tmp_path: Path,
) -> None:
    vault = _vault(tmp_path)
    vault.create_trace("known-good")
    db_path = tmp_path / "authority.sqlite3"

    _force_meta(db_path, "tip_hash", "f" * 64)

    conn = sqlite3.connect(db_path)
    before_traces = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    before_evidence = conn.execute(
        "SELECT COUNT(*) FROM evidence_records"
    ).fetchone()[0]
    conn.close()

    with pytest.raises(LedgerIntegrityError):
        vault.create_trace("must-not-survive")

    conn = sqlite3.connect(db_path)
    after_traces = conn.execute("SELECT COUNT(*) FROM traces").fetchone()[0]
    after_evidence = conn.execute(
        "SELECT COUNT(*) FROM evidence_records"
    ).fetchone()[0]
    conn.close()

    assert after_traces == before_traces
    assert after_evidence == before_evidence


def test_forged_tip_blocks_append_without_advancing_authority(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    trace_id = vault.create_trace("tip")
    vault.append_step(_step(trace_id, 0))
    db_path = tmp_path / "authority.sqlite3"

    _force_meta(db_path, "tip_hash", "e" * 64)
    before_records = len(vault.get_evidence_records())

    with pytest.raises(LedgerIntegrityError):
        vault.append_step(_step(trace_id, 1))

    assert len(vault.get_evidence_records()) == before_records
    result = vault.verify_chain()
    assert result.ok is False
    assert "tip hash" in (result.failure_reason or "")


def test_tip_sequence_and_genesis_metadata_are_verified(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    trace_id = vault.create_trace("meta")
    vault.append_step(_step(trace_id, 0))
    db_path = tmp_path / "authority.sqlite3"

    meta = vault.get_chain_metadata()
    _force_meta(db_path, "tip_seq", int(meta["tip_seq"]) + 100)
    result = vault.verify_chain()
    assert result.ok is False
    assert "tip sequence" in (result.failure_reason or "")

    _force_meta(db_path, "tip_seq", int(meta["tip_seq"]))
    _force_meta(db_path, "genesis_hash", "a" * 64)
    result = vault.verify_chain()
    assert result.ok is False
    assert "genesis" in (result.failure_reason or "")


def test_schema_and_hash_versions_are_authenticated(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    trace_id = vault.create_trace("versions")
    vault.append_step(_step(trace_id, 0))
    db_path = tmp_path / "authority.sqlite3"

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER trg_evidence_no_update")
    conn.execute(
        "UPDATE evidence_records SET schema_version=? WHERE seq=1",
        (SCHEMA_VERSION + 1,),
    )
    conn.commit()
    conn.close()

    result = vault.verify_chain()
    assert result.ok is False
    assert "schema_version" in (result.failure_reason or "")


def test_hash_changes_when_sequence_identity_is_tampered(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    trace_id = vault.create_trace("seq")
    vault.append_step(_step(trace_id, 0))
    db_path = tmp_path / "authority.sqlite3"

    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER trg_evidence_no_update")
    conn.execute("UPDATE evidence_records SET seq=99 WHERE seq=2")
    conn.commit()
    conn.close()

    result = vault.verify_chain()
    assert result.ok is False
    assert "sequence gap" in (result.failure_reason or "")


def test_step_order_is_serialized_inside_write_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "race.sqlite3"
    vault_a = ProofVault(db_path=db_path)
    vault_b = ProofVault(db_path=db_path)
    trace_id = vault_a.create_trace("race")

    barrier = threading.Barrier(2)
    successes: list[int] = []
    failures: list[int] = []

    def writer(vault: ProofVault, index: int) -> None:
        try:
            barrier.wait(timeout=5)
            vault.append_step(_step(trace_id, index))
            successes.append(index)
        except Exception:
            failures.append(index)

    first = threading.Thread(target=writer, args=(vault_a, 1))
    second = threading.Thread(target=writer, args=(vault_b, 2))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert successes
    records = [
        r
        for r in vault_a.get_evidence_records(trace_id)
        if r.step_index is not None and r.provenance == PROVENANCE_VERIFIED
    ]
    committed_indices = [int(r.step_index) for r in records]
    assert committed_indices == sorted(committed_indices)
    assert committed_indices != [2, 1]
    assert vault_a.verify_chain().ok is True


def test_receipt_mutation_cannot_verify_against_healthy_vault(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    trace_id = vault.create_trace("receipt")
    vault.append_step(_step(trace_id, 0))
    vault.append_step(_step(trace_id, 1))

    builder = ReceiptBuilder(vault)
    receipt = builder.build_receipt(trace_id)
    assert receipt.verified is True

    mutated_action = copy.deepcopy(receipt)
    mutated_action.steps[0].action = "MUTATED"
    assert builder.verify_chain(mutated_action) is False

    mutated_hash = copy.deepcopy(receipt)
    mutated_hash.steps[0].step_hash = "0" * 64
    assert builder.verify_chain(mutated_hash) is False

    mutated_tip = copy.deepcopy(receipt)
    mutated_tip.chain_tip = "1" * 64
    assert builder.verify_chain(mutated_tip) is False

    omitted_membership = copy.deepcopy(receipt)
    omitted_membership.trace_evidence_seqs = omitted_membership.trace_evidence_seqs[:-1]
    assert builder.verify_chain(omitted_membership) is False


def test_verified_replay_fails_closed_after_tamper(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    trace_id = vault.create_trace("replay")
    event = vault.append_step(_step(trace_id, 0))
    builder = ReceiptBuilder(vault)
    assert builder.replay(trace_id)

    db_path = tmp_path / "authority.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TRIGGER trg_evidence_no_update")
    conn.execute(
        "UPDATE evidence_records SET canonical_payload='{}' WHERE seq=?",
        (event.seq,),
    )
    conn.commit()
    conn.close()

    with pytest.raises(LedgerIntegrityError, match="verified replay unavailable"):
        builder.replay(trace_id)


def test_legacy_trace_metadata_is_snapshotted_and_never_certified(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    _legacy_db(db_path)
    vault = ProofVault(db_path=db_path)
    builder = ReceiptBuilder(vault)

    legacy_records = [
        r for r in vault.get_evidence_records() if r.provenance == PROVENANCE_LEGACY
    ]
    assert len(legacy_records) == 1

    snapshot = vault.get_legacy_trace_snapshot("legacy-trace")
    assert snapshot is not None
    assert snapshot["provenance"] == PROVENANCE_LEGACY
    assert snapshot["payload"]["objective"] == "legacy objective"

    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE traces SET objective='mutated projection' WHERE trace_id='legacy-trace'"
    )
    conn.commit()
    conn.close()

    receipt = builder.build_receipt("legacy-trace")
    assert receipt.objective == "legacy objective"
    assert receipt.provenance == PROVENANCE_LEGACY
    assert receipt.verified is False


def test_legacy_row_after_verified_chain_is_rejected(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    trace_id = vault.create_trace("verified")
    vault.append_step(_step(trace_id, 0))
    db_path = tmp_path / "authority.sqlite3"

    conn = sqlite3.connect(db_path)
    next_seq = conn.execute(
        "SELECT MAX(seq) + 1 FROM evidence_records"
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO evidence_records(
            seq, evidence_type, trace_id, step_index, canonical_payload,
            prev_hash, record_hash, timestamp, schema_version,
            hash_version, provenance
        ) VALUES (?, 'step.legacy', 'late-legacy', 0, '{}', '', '', 5.0, ?, ?, ?)
        """,
        (next_seq, SCHEMA_VERSION, HASH_VERSION, PROVENANCE_LEGACY),
    )
    conn.commit()
    conn.close()

    result = vault.verify_chain()
    assert result.ok is False
    assert "legacy evidence appears after VERIFIED" in (result.failure_reason or "")


def test_legacy_checkpoint_and_new_chain_survive_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "upgrade.sqlite3"
    _legacy_db(db_path)
    vault = ProofVault(db_path=db_path)
    checkpoint_before = vault.get_chain_metadata()["checkpoint_hash"]
    assert checkpoint_before

    new_trace = vault.create_trace("post-migration")
    vault.append_step(_step(new_trace, 0))
    assert vault.verify_chain().ok is True

    restarted = ProofVault(db_path=db_path)
    assert restarted.verify_chain().ok is True
    assert restarted.get_chain_metadata()["checkpoint_hash"] == checkpoint_before

    legacy_receipt = ReceiptBuilder(restarted).build_receipt("legacy-trace")
    new_receipt = ReceiptBuilder(restarted).build_receipt(new_trace)
    assert legacy_receipt.verified is False
    assert new_receipt.verified is True


def test_authority_event_uses_same_chain_and_versions(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    trace_id = vault.create_trace("intent")
    event = vault.append_authority_event(
        "approval_issued",
        trace_id,
        {"intent_id": "i-1", "action_digest": "abc"},
        timestamp=1_700_200_000.0,
    )
    assert event.evidence_type == "authority.approval_issued"
    assert event.schema_version == SCHEMA_VERSION
    assert event.hash_version == HASH_VERSION
    assert event.provenance == PROVENANCE_VERIFIED
    assert vault.verify_chain().ok is True
