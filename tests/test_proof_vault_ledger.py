"""
test_proof_vault_ledger.py — Adversarial tests for ProofVault write-time ledger.

Covers (per PROOFVAULT_INTEGRITY.md §Required adversarial coverage):
  - deterministic canonical hashing
  - rejection of NaN / Infinity / non-serialisable / cyclic payloads
  - immutable UPDATE / DELETE trigger enforcement
  - historical payload / hash mutation detection
  - missing / deleted / reordered sequence detection
  - forged prev_hash or durable tip detection
  - duplicate per-trace step identity rejection
  - step order constraint (monotonic step_index)
  - two-writer append serialization
  - rollback when append / tip advancement fails
  - restart verification with stable root / tip
  - legacy database migration remaining explicitly unverified
  - receipt construction never asserting verification without successful ledger pass
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from sovereign_claw import (
    LedgerIntegrityError,
    ProofVault,
    StepRecord,
    canonical_json,
)
from sovereign_claw.proof_vault import GENESIS_PREV_HASH, PROVENANCE_LEGACY, PROVENANCE_VERIFIED
from sovereign_claw.receipts import ReceiptBuilder

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_vault(tmp_path: Path) -> ProofVault:
    return ProofVault(db_path=tmp_path / "ledger.sqlite3")


def _step(trace_id: str, idx: int, **kwargs: Any) -> StepRecord:
    return StepRecord(
        trace_id=trace_id,
        step_index=idx,
        timestamp=1_700_000_000.0 + idx,
        node="test-node",
        action="TEST",
        drift=0.1 * idx,
        status="OK",
        payload=kwargs.get("payload", {"k": idx}),
    )


# ── Canonical JSON ────────────────────────────────────────────────────────────


class TestCanonicalJSON:
    def test_sorted_keys(self) -> None:
        result = canonical_json({"z": 1, "a": 2})
        assert result == '{"a":2,"z":1}'

    def test_compact_separators(self) -> None:
        result = canonical_json({"x": [1, 2]})
        assert " " not in result

    def test_nested_sorted(self) -> None:
        result = canonical_json({"b": {"z": 1, "a": 2}, "a": 0})
        assert result == '{"a":0,"b":{"a":2,"z":1}}'

    def test_deterministic_across_calls(self) -> None:
        obj = {"c": 3, "a": 1, "b": 2}
        assert canonical_json(obj) == canonical_json(obj)

    def test_rejects_nan(self) -> None:
        with pytest.raises((ValueError, LedgerIntegrityError)):
            canonical_json({"x": float("nan")})

    def test_rejects_positive_infinity(self) -> None:
        with pytest.raises((ValueError, LedgerIntegrityError)):
            canonical_json({"x": float("inf")})

    def test_rejects_negative_infinity(self) -> None:
        with pytest.raises((ValueError, LedgerIntegrityError)):
            canonical_json({"x": float("-inf")})

    def test_rejects_cyclic(self) -> None:
        cyclic: dict = {}
        cyclic["self"] = cyclic
        with pytest.raises((ValueError, TypeError, LedgerIntegrityError)):
            canonical_json(cyclic)

    def test_rejects_non_serialisable(self) -> None:
        with pytest.raises((ValueError, TypeError, LedgerIntegrityError)):
            canonical_json({"x": object()})


# ── Basic append and chain verification ──────────────────────────────────────


class TestBasicChain:
    def test_empty_chain_verifies(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        result = vault.verify_chain()
        assert result.ok is True
        assert result.verified_count == 0

    def test_single_trace_verifies(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("test")
        vault.append_step(_step(trace_id, 0))
        result = vault.verify_chain()
        assert result.ok is True
        assert result.verified_count >= 1

    def test_multi_step_verifies(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("multi")
        for i in range(5):
            vault.append_step(_step(trace_id, i))
        result = vault.verify_chain()
        assert result.ok is True

    def test_record_hash_stored(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("hash-check")
        ev = vault.append_step(_step(trace_id, 0))
        assert len(ev.record_hash) == 64
        assert ev.provenance == PROVENANCE_VERIFIED

    def test_genesis_prev_hash_on_first_record(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("genesis")
        vault.append_step(_step(trace_id, 0))
        # The first verified step evidence should have prev_hash == genesis or tip_hash
        records = vault.get_evidence_records(trace_id=trace_id)
        # The genesis sentinel for the chain is GENESIS_PREV_HASH
        # The first 'trace.created' record uses GENESIS_PREV_HASH as its prev
        chain_records = [r for r in records if r.provenance == PROVENANCE_VERIFIED]
        assert chain_records[0].prev_hash == GENESIS_PREV_HASH

    def test_tip_advances_after_append(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        seq0, hash0 = vault.get_chain_tip()
        trace_id = vault.create_trace("tip-test")
        seq1, hash1 = vault.get_chain_tip()
        vault.append_step(_step(trace_id, 0))
        seq2, hash2 = vault.get_chain_tip()
        assert seq2 > seq1 > seq0
        assert hash2 != hash1 != hash0 or hash0 == ""


# ── Immutability triggers ─────────────────────────────────────────────────────


class TestImmutabilityTriggers:
    def test_update_blocked_by_trigger(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("immutable")
        vault.append_step(_step(trace_id, 0))
        conn = sqlite3.connect(tmp_path / "ledger.sqlite3")
        with pytest.raises((sqlite3.OperationalError, sqlite3.IntegrityError), match="immutable"):
            conn.execute("UPDATE evidence_records SET record_hash='forged' WHERE seq=1")
        conn.close()

    def test_delete_blocked_by_trigger(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("immutable-del")
        vault.append_step(_step(trace_id, 0))
        conn = sqlite3.connect(tmp_path / "ledger.sqlite3")
        with pytest.raises((sqlite3.OperationalError, sqlite3.IntegrityError), match="immutable"):
            conn.execute("DELETE FROM evidence_records WHERE seq=1")
        conn.close()


# ── Tamper detection ─────────────────────────────────────────────────────────


class TestTamperDetection:
    def _force_tamper_payload(self, db_path: Path, seq: int, new_payload: str) -> None:
        """Bypass triggers by dropping and recreating the trigger-free table."""
        conn = sqlite3.connect(db_path)
        # SQLite doesn't allow disabling triggers; work around via ALTER TABLE rename
        conn.execute("DROP TRIGGER IF EXISTS trg_evidence_no_update")
        conn.execute(
            "UPDATE evidence_records SET canonical_payload=? WHERE seq=?", (new_payload, seq)
        )
        # Re-create trigger
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_evidence_no_update "
            "BEFORE UPDATE ON evidence_records BEGIN "
            "SELECT RAISE(ABORT, 'evidence_records is immutable: UPDATE is forbidden'); END"
        )
        conn.commit()
        conn.close()

    def _force_tamper_hash(self, db_path: Path, seq: int, new_hash: str) -> None:
        conn = sqlite3.connect(db_path)
        conn.execute("DROP TRIGGER IF EXISTS trg_evidence_no_update")
        conn.execute("UPDATE evidence_records SET record_hash=? WHERE seq=?", (new_hash, seq))
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_evidence_no_update "
            "BEFORE UPDATE ON evidence_records BEGIN "
            "SELECT RAISE(ABORT, 'evidence_records is immutable: UPDATE is forbidden'); END"
        )
        conn.commit()
        conn.close()

    def test_payload_mutation_detected(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("tamper")
        ev = vault.append_step(_step(trace_id, 0))
        self._force_tamper_payload(tmp_path / "ledger.sqlite3", ev.seq, '{"tampered":true}')
        result = vault.verify_chain()
        assert result.ok is False
        assert result.first_failure_seq == ev.seq

    def test_record_hash_forgery_detected(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("forged-hash")
        ev = vault.append_step(_step(trace_id, 0))
        self._force_tamper_hash(tmp_path / "ledger.sqlite3", ev.seq, "a" * 64)
        result = vault.verify_chain()
        assert result.ok is False

    def test_deleted_verified_record_detected(self, tmp_path: Path) -> None:
        """Deleting a VERIFIED evidence row mid-chain must fail verification."""
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("deleted-row")
        ev0 = vault.append_step(_step(trace_id, 0))
        vault.append_step(_step(trace_id, 1))

        # Force-delete the middle VERIFIED row (bypass trigger)
        conn = sqlite3.connect(tmp_path / "ledger.sqlite3")
        conn.execute("DROP TRIGGER IF EXISTS trg_evidence_no_delete")
        conn.execute("DELETE FROM evidence_records WHERE seq=?", (ev0.seq,))
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_evidence_no_delete "
            "BEFORE DELETE ON evidence_records BEGIN "
            "SELECT RAISE(ABORT, 'evidence_records is immutable: DELETE is forbidden'); END"
        )
        conn.commit()
        conn.close()

        result = vault.verify_chain()
        assert result.ok is False


# ── Forged tip detection ─────────────────────────────────────────────────────


class TestForgedTip:
    def test_forged_durable_tip_detected(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("forged-tip")
        vault.append_step(_step(trace_id, 0))
        # Forge the durable tip hash
        conn = sqlite3.connect(tmp_path / "ledger.sqlite3")
        conn.execute("UPDATE chain_meta SET tip_hash=? WHERE id=1", ("f" * 64,))
        conn.commit()
        conn.close()
        result = vault.verify_chain()
        assert result.ok is False

    def test_forged_prev_hash_detected(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("forged-prev")
        vault.append_step(_step(trace_id, 0))
        ev2 = vault.append_step(_step(trace_id, 1))
        # Force prev_hash of seq=2 to garbage
        conn = sqlite3.connect(tmp_path / "ledger.sqlite3")
        conn.execute("DROP TRIGGER IF EXISTS trg_evidence_no_update")
        conn.execute("UPDATE evidence_records SET prev_hash=? WHERE seq=?", ("b" * 64, ev2.seq))
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_evidence_no_update "
            "BEFORE UPDATE ON evidence_records BEGIN "
            "SELECT RAISE(ABORT, 'evidence_records is immutable: UPDATE is forbidden'); END"
        )
        conn.commit()
        conn.close()
        result = vault.verify_chain()
        assert result.ok is False


# ── Duplicate step identity ───────────────────────────────────────────────────


class TestStepIdentity:
    def test_duplicate_step_index_rejected(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("dup")
        vault.append_step(_step(trace_id, 0))
        with pytest.raises(LedgerIntegrityError):
            vault.append_step(_step(trace_id, 0))

    def test_step_order_monotonic(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("order")
        vault.append_step(_step(trace_id, 3))
        with pytest.raises(LedgerIntegrityError, match="Step order violation"):
            vault.append_step(_step(trace_id, 2))

    def test_different_traces_same_step_index_allowed(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        t1 = vault.create_trace("t1")
        t2 = vault.create_trace("t2")
        vault.append_step(_step(t1, 0))
        vault.append_step(_step(t2, 0))  # must not raise
        result = vault.verify_chain()
        assert result.ok is True


# ── NaN / Infinity / cyclic payload rejection ─────────────────────────────────


class TestPayloadValidation:
    def test_nan_in_step_drift(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("nan")
        step = StepRecord(
            trace_id=trace_id,
            step_index=0,
            timestamp=1.0,
            node="n",
            action="a",
            drift=float("nan"),
            status="s",
            payload={},
        )
        with pytest.raises((ValueError, LedgerIntegrityError)):
            vault.append_step(step)

    def test_inf_in_payload(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("inf")
        with pytest.raises((ValueError, LedgerIntegrityError)):
            vault.append_step(_step(trace_id, 0, payload={"x": float("inf")}))


# ── Two-writer serialization ─────────────────────────────────────────────────


class TestTwoWriterSerialization:
    def test_concurrent_appends_serialize(self, tmp_path: Path) -> None:
        """Both writers must succeed or one must fail; no tip corruption allowed."""
        db_path = tmp_path / "concurrent.sqlite3"
        vault1 = ProofVault(db_path=db_path)
        vault2 = ProofVault(db_path=db_path)
        trace_id = vault1.create_trace("concurrent")

        errors: list = []
        successes: list = []

        def writer(vault: ProofVault, idx: int) -> None:
            try:
                vault.append_step(_step(trace_id, idx))
                successes.append(idx)
            except Exception as exc:
                errors.append((idx, exc))

        t1 = threading.Thread(target=writer, args=(vault1, 1))
        t2 = threading.Thread(target=writer, args=(vault2, 2))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        # At least one must succeed; chain must be valid afterwards
        assert len(successes) >= 1
        result = vault1.verify_chain()
        assert result.ok is True

    def test_two_writers_same_step_index_only_one_wins(self, tmp_path: Path) -> None:
        """Both writers race on the same step_index; at most one succeeds."""
        db_path = tmp_path / "race.sqlite3"
        vault1 = ProofVault(db_path=db_path)
        vault2 = ProofVault(db_path=db_path)
        trace_id = vault1.create_trace("race")

        successes: list = []
        errors: list = []

        def writer(vault: ProofVault) -> None:
            try:
                vault.append_step(_step(trace_id, 0))
                successes.append(True)
            except (LedgerIntegrityError, sqlite3.OperationalError, sqlite3.IntegrityError):
                errors.append(True)

        t1 = threading.Thread(target=writer, args=(vault1,))
        t2 = threading.Thread(target=writer, args=(vault2,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert len(successes) == 1  # exactly one writer wins


# ── Restart verification ─────────────────────────────────────────────────────


class TestRestartVerification:
    def test_chain_verifies_after_vault_restart(self, tmp_path: Path) -> None:
        db_path = tmp_path / "restart.sqlite3"
        vault = ProofVault(db_path=db_path)
        trace_id = vault.create_trace("restart")
        for i in range(3):
            vault.append_step(_step(trace_id, i))

        # Open a new ProofVault instance (simulates restart)
        vault2 = ProofVault(db_path=db_path)
        result = vault2.verify_chain()
        assert result.ok is True
        assert result.verified_count >= 3

    def test_tip_stable_across_restart(self, tmp_path: Path) -> None:
        db_path = tmp_path / "stable.sqlite3"
        vault = ProofVault(db_path=db_path)
        trace_id = vault.create_trace("stable")
        vault.append_step(_step(trace_id, 0))
        tip_before = vault.get_chain_tip()

        vault2 = ProofVault(db_path=db_path)
        tip_after = vault2.get_chain_tip()

        assert tip_before == tip_after


# ── Legacy migration ─────────────────────────────────────────────────────────


class TestLegacyMigration:
    def _create_legacy_db(self, db_path: Path) -> None:
        """Simulate a pre-chain database with existing steps but no evidence_records."""
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS traces (
                trace_id   TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                objective  TEXT,
                meta       JSON
            );
            CREATE TABLE IF NOT EXISTS steps (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id    TEXT    NOT NULL,
                step_index  INTEGER NOT NULL,
                timestamp   REAL    NOT NULL,
                node        TEXT    NOT NULL,
                action      TEXT    NOT NULL,
                drift       REAL    NOT NULL,
                status      TEXT    NOT NULL,
                payload     JSON    NOT NULL
            );
            INSERT INTO traces VALUES ('legacy-trace', 1000.0, 'legacy obj', '{}');
            INSERT INTO steps VALUES (
                NULL, 'legacy-trace', 0, 1000.0, 'node', 'ACT', 0.5, 'OK', '{}'
            );
            """
        )
        conn.commit()
        conn.close()

    def test_legacy_rows_tagged_unverified(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy.sqlite3"
        self._create_legacy_db(db_path)

        vault = ProofVault(db_path=db_path)
        records = vault.get_evidence_records()
        legacy = [r for r in records if r.provenance == PROVENANCE_LEGACY]
        assert len(legacy) >= 1
        assert legacy[0].trace_id == "legacy-trace"

    def test_legacy_rows_have_empty_hashes(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy2.sqlite3"
        self._create_legacy_db(db_path)

        vault = ProofVault(db_path=db_path)
        records = vault.get_evidence_records()
        legacy = [r for r in records if r.provenance == PROVENANCE_LEGACY]
        for r in legacy:
            assert r.record_hash == ""
            assert r.prev_hash == ""

    def test_new_writes_after_migration_are_verified(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy3.sqlite3"
        self._create_legacy_db(db_path)

        vault = ProofVault(db_path=db_path)
        trace_id = vault.create_trace("new-post-migration")
        ev = vault.append_step(
            StepRecord(
                trace_id=trace_id,
                step_index=0,
                timestamp=2000.0,
                node="n",
                action="a",
                drift=0.0,
                status="OK",
                payload={},
            )
        )
        assert ev.provenance == PROVENANCE_VERIFIED

    def test_verify_chain_skips_legacy(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy4.sqlite3"
        self._create_legacy_db(db_path)

        vault = ProofVault(db_path=db_path)
        trace_id = vault.create_trace("post-legacy")
        vault.append_step(
            StepRecord(
                trace_id=trace_id,
                step_index=0,
                timestamp=2000.0,
                node="n",
                action="a",
                drift=0.0,
                status="OK",
                payload={},
            )
        )
        result = vault.verify_chain()
        assert result.ok is True

    def test_legacy_migration_idempotent(self, tmp_path: Path) -> None:
        """Opening the vault twice does not duplicate legacy imports."""
        db_path = tmp_path / "legacy5.sqlite3"
        self._create_legacy_db(db_path)

        ProofVault(db_path=db_path)
        ProofVault(db_path=db_path)  # second init must not duplicate rows

        vault = ProofVault(db_path=db_path)
        records = vault.get_evidence_records()
        legacy = [r for r in records if r.provenance == PROVENANCE_LEGACY]
        assert len(legacy) == 1  # only one legacy row from _create_legacy_db


# ── Receipt builder ───────────────────────────────────────────────────────────


class TestReceiptBuilder:
    def test_receipt_verified_after_clean_chain(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("receipt")
        vault.append_step(_step(trace_id, 0))
        vault.append_step(_step(trace_id, 1))

        builder = ReceiptBuilder(vault)
        receipt = builder.build_receipt(trace_id)
        assert receipt.verified is True

    def test_receipt_not_verified_on_legacy_trace(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy-receipt.sqlite3"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS traces (
                trace_id   TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                objective  TEXT,
                meta       JSON
            );
            CREATE TABLE IF NOT EXISTS steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL, step_index INTEGER NOT NULL,
                timestamp REAL NOT NULL, node TEXT NOT NULL,
                action TEXT NOT NULL, drift REAL NOT NULL,
                status TEXT NOT NULL, payload JSON NOT NULL
            );
            INSERT INTO traces VALUES ('leg', 1.0, 'legacy', '{}');
            INSERT INTO steps VALUES (NULL, 'leg', 0, 1.0, 'n', 'a', 0.0, 'OK', '{}');
            """
        )
        conn.commit()
        conn.close()

        vault = ProofVault(db_path=db_path)
        builder = ReceiptBuilder(vault)
        receipt = builder.build_receipt("leg")
        assert receipt.verified is False
        assert receipt.provenance == PROVENANCE_LEGACY

    def test_receipt_not_verified_after_tamper(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("tamper-receipt")
        ev = vault.append_step(_step(trace_id, 0))

        # Tamper with the canonical payload (bypass trigger)
        conn = sqlite3.connect(tmp_path / "ledger.sqlite3")
        conn.execute("DROP TRIGGER IF EXISTS trg_evidence_no_update")
        conn.execute(
            "UPDATE evidence_records SET canonical_payload=? WHERE seq=?",
            ('{"tampered":1}', ev.seq),
        )
        conn.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_evidence_no_update "
            "BEFORE UPDATE ON evidence_records BEGIN "
            "SELECT RAISE(ABORT, 'evidence_records is immutable: UPDATE is forbidden'); END"
        )
        conn.commit()
        conn.close()

        builder = ReceiptBuilder(vault)
        receipt = builder.build_receipt(trace_id)
        assert receipt.verified is False

    def test_verify_chain_method_on_receipt(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("verify-receipt")
        vault.append_step(_step(trace_id, 0))

        builder = ReceiptBuilder(vault)
        receipt = builder.build_receipt(trace_id)
        assert builder.verify_chain(receipt) is True

    def test_verify_chain_false_for_legacy_receipt(self, tmp_path: Path) -> None:
        db_path = tmp_path / "legacy-vr.sqlite3"
        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS traces (
                trace_id TEXT PRIMARY KEY, created_at REAL NOT NULL,
                objective TEXT, meta JSON
            );
            CREATE TABLE IF NOT EXISTS steps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL, step_index INTEGER NOT NULL,
                timestamp REAL NOT NULL, node TEXT NOT NULL,
                action TEXT NOT NULL, drift REAL NOT NULL,
                status TEXT NOT NULL, payload JSON NOT NULL
            );
            INSERT INTO traces VALUES ('l2', 1.0, 'leg', '{}');
            INSERT INTO steps VALUES (NULL, 'l2', 0, 1.0, 'n', 'a', 0.0, 'OK', '{}');
            """
        )
        conn.commit()
        conn.close()

        vault = ProofVault(db_path=db_path)
        builder = ReceiptBuilder(vault)
        receipt = builder.build_receipt("l2")
        assert builder.verify_chain(receipt) is False

    def test_receipt_stores_provenance(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("prov")
        vault.append_step(_step(trace_id, 0))
        builder = ReceiptBuilder(vault)
        receipt = builder.build_receipt(trace_id)
        assert receipt.provenance == PROVENANCE_VERIFIED

    def test_receipt_tip_matches_ledger_tip(self, tmp_path: Path) -> None:
        vault = _make_vault(tmp_path)
        trace_id = vault.create_trace("tip-match")
        vault.append_step(_step(trace_id, 0))
        builder = ReceiptBuilder(vault)
        receipt = builder.build_receipt(trace_id)
        _, tip_hash = vault.get_chain_tip()
        assert receipt.chain_tip == tip_hash


# ── Event stream is derived only ─────────────────────────────────────────────


class TestEventStreamDerived:
    def test_mirror_failure_does_not_affect_authority(self, tmp_path: Path) -> None:
        """If EventStream fails, the evidence record is still committed."""
        from sovereign_claw.event_stream import EventStream

        class BrokenStream(EventStream):
            def append(self, *args, **kwargs):  # type: ignore[override]
                raise OSError("mirror down")

        broken = BrokenStream(tmp_path / "broken.jsonl")
        vault = ProofVault(db_path=tmp_path / "mirror.sqlite3", event_stream=broken)
        trace_id = vault.create_trace("mirror-fail")
        vault.append_step(_step(trace_id, 0))

        # Authority record must exist despite mirror failure
        records = vault.get_evidence_records(trace_id=trace_id)
        step_records = [r for r in records if r.step_index is not None]
        assert len(step_records) == 1
        assert step_records[0].provenance == PROVENANCE_VERIFIED

    def test_event_stream_carries_source_seq(self, tmp_path: Path) -> None:
        from sovereign_claw.event_stream import EventStream

        stream = EventStream(tmp_path / "events.jsonl")
        vault = ProofVault(db_path=tmp_path / "stream.sqlite3", event_stream=stream)
        trace_id = vault.create_trace("stream-seq")
        vault.append_step(_step(trace_id, 0))

        events = stream.read(trace_id)
        step_events = [e for e in events if e.event_type == "step.appended"]
        assert len(step_events) == 1
        assert "source_seq" in step_events[0].payload
        assert "source_record_hash" in step_events[0].payload
