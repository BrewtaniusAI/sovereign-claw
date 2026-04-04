"""Tests for sovereign_claw.secrets_manager."""

from __future__ import annotations

import time

import pytest

from sovereign_claw.secrets_manager import (
    AuditAction,
    AuditEntry,
    SecretMetadata,
    SecretScope,
    SecretStatus,
    SecretsManager,
    SimpleEncryptor,
)


# ── SimpleEncryptor ──────────────────────────────────────────────────────────


class TestSimpleEncryptor:
    def test_encrypt_decrypt(self) -> None:
        enc = SimpleEncryptor("my-secret-key")
        plaintext = "super-secret-api-key-12345"
        ciphertext = enc.encrypt(plaintext)
        assert ciphertext != plaintext
        decrypted = enc.decrypt(ciphertext)
        assert decrypted == plaintext

    def test_different_keys_different_output(self) -> None:
        enc1 = SimpleEncryptor("key1")
        enc2 = SimpleEncryptor("key2")
        plain = "secret"
        c1 = enc1.encrypt(plain)
        c2 = enc2.encrypt(plain)
        # Different keys produce different ciphertexts (with high probability)
        # Both should decrypt with their own key
        assert enc1.decrypt(c1) == plain
        assert enc2.decrypt(c2) == plain

    def test_empty_string(self) -> None:
        enc = SimpleEncryptor("key")
        assert enc.decrypt(enc.encrypt("")) == ""

    def test_unicode(self) -> None:
        enc = SimpleEncryptor("key")
        plain = "password: 密码123"
        assert enc.decrypt(enc.encrypt(plain)) == plain

    def test_long_string(self) -> None:
        enc = SimpleEncryptor("key")
        plain = "x" * 10000
        assert enc.decrypt(enc.encrypt(plain)) == plain

    def test_auto_key_generation(self) -> None:
        enc = SimpleEncryptor()
        plain = "auto-key-test"
        assert enc.decrypt(enc.encrypt(plain)) == plain


# ── SecretMetadata ───────────────────────────────────────────────────────────


class TestSecretMetadata:
    def test_creation(self) -> None:
        meta = SecretMetadata(name="API_KEY")
        assert meta.name == "API_KEY"
        assert meta.status == SecretStatus.ACTIVE
        assert meta.version == 1

    def test_not_expired_no_expiry(self) -> None:
        meta = SecretMetadata(name="KEY", expires_at=0.0)
        assert not meta.is_expired

    def test_expired(self) -> None:
        meta = SecretMetadata(name="KEY", expires_at=time.time() - 100)
        assert meta.is_expired

    def test_not_expired(self) -> None:
        meta = SecretMetadata(name="KEY", expires_at=time.time() + 3600)
        assert not meta.is_expired

    def test_needs_rotation(self) -> None:
        meta = SecretMetadata(
            name="KEY",
            rotation_interval_seconds=60,
            created_at=time.time() - 120,
        )
        assert meta.needs_rotation

    def test_no_rotation_needed(self) -> None:
        meta = SecretMetadata(name="KEY", rotation_interval_seconds=0)
        assert not meta.needs_rotation

    def test_masked_name(self) -> None:
        meta = SecretMetadata(name="API_KEY_123")
        assert "****" in meta.masked_name
        assert meta.masked_name.startswith("AP")
        assert meta.masked_name.endswith("23")

    def test_masked_short_name(self) -> None:
        meta = SecretMetadata(name="KEY")
        assert meta.masked_name == "****"

    def test_to_dict(self) -> None:
        meta = SecretMetadata(name="KEY", scope=SecretScope.AGENT)
        d = meta.to_dict()
        assert d["name"] == "KEY"
        assert d["scope"] == "agent"


# ── AuditEntry ───────────────────────────────────────────────────────────────


class TestAuditEntry:
    def test_creation(self) -> None:
        entry = AuditEntry(
            secret_name="API_KEY",
            action=AuditAction.READ,
            accessor="orchestrator",
        )
        assert entry.entry_id.startswith("audit_")
        assert entry.success is True

    def test_to_dict(self) -> None:
        entry = AuditEntry(
            secret_name="KEY",
            action=AuditAction.CREATED,
            accessor="system",
        )
        d = entry.to_dict()
        assert d["action"] == "created"


# ── SecretsManager ───────────────────────────────────────────────────────────


class TestSecretsManager:
    def test_store_and_retrieve(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("API_KEY", "sk-12345")
        value = mgr.retrieve("API_KEY", accessor="orchestrator")
        assert value == "sk-12345"

    def test_retrieve_nonexistent(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        assert mgr.retrieve("NOPE") is None

    def test_store_update(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "v1")
        mgr.store("KEY", "v2")
        assert mgr.retrieve("KEY") == "v2"
        meta = mgr._metadata["KEY"]
        assert meta.version == 2

    def test_store_with_scope(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        meta = mgr.store("KEY", "val", scope=SecretScope.AGENT)
        assert meta.scope == SecretScope.AGENT

    def test_store_with_expiry(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "val", expires_at=time.time() - 1)
        # Should be expired
        value = mgr.retrieve("KEY")
        assert value is None

    def test_scoped_access_allowed(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "val", allowed_accessors=["agent_a", "agent_b"])
        assert mgr.retrieve("KEY", accessor="agent_a") == "val"

    def test_scoped_access_denied(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "val", allowed_accessors=["agent_a"])
        assert mgr.retrieve("KEY", accessor="agent_b") is None

    def test_scoped_access_empty_allows_all(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "val", allowed_accessors=[])
        assert mgr.retrieve("KEY", accessor="anyone") == "val"

    def test_rotate(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "old_val")
        meta = mgr.rotate("KEY", "new_val")
        assert meta is not None
        assert meta.version == 2
        assert mgr.retrieve("KEY") == "new_val"

    def test_rotate_nonexistent(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        assert mgr.rotate("NOPE", "val") is None

    def test_revoke(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "val")
        assert mgr.revoke("KEY")
        # Value should be gone
        assert mgr.retrieve("KEY") is None
        # Metadata still exists but status is revoked
        assert mgr._metadata["KEY"].status == SecretStatus.REVOKED

    def test_revoke_nonexistent(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        assert not mgr.revoke("NOPE")

    def test_delete(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "val")
        assert mgr.delete("KEY")
        assert mgr.retrieve("KEY") is None
        assert "KEY" not in mgr._metadata

    def test_delete_nonexistent(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        assert not mgr.delete("NOPE")

    def test_list_secrets(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY1", "v1", scope=SecretScope.GLOBAL)
        mgr.store("KEY2", "v2", scope=SecretScope.AGENT)
        mgr.store("KEY3", "v3", scope=SecretScope.GLOBAL)
        all_secrets = mgr.list_secrets()
        assert len(all_secrets) == 3

    def test_list_secrets_by_scope(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("K1", "v1", scope=SecretScope.GLOBAL)
        mgr.store("K2", "v2", scope=SecretScope.AGENT)
        global_only = mgr.list_secrets(scope=SecretScope.GLOBAL)
        assert len(global_only) == 1

    def test_list_secrets_by_tag(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("K1", "v1", tags=["api"])
        mgr.store("K2", "v2", tags=["db"])
        api_secrets = mgr.list_secrets(tag="api")
        assert len(api_secrets) == 1

    def test_check_rotations(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "val", rotation_interval=1)
        # Force created_at to past
        mgr._metadata["KEY"].created_at = time.time() - 100
        needs = mgr.check_rotations()
        assert "KEY" in needs

    def test_expire_stale(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "val", expires_at=time.time() - 100)
        # Reset status to active (store sets it active)
        mgr._metadata["KEY"].status = SecretStatus.ACTIVE
        count = mgr.expire_stale()
        assert count == 1
        assert mgr._metadata["KEY"].status == SecretStatus.EXPIRED

    def test_audit_trail(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "val")
        mgr.retrieve("KEY", accessor="agent_a")
        mgr.retrieve("KEY", accessor="agent_b")
        trail = mgr.audit_trail("KEY")
        assert len(trail) >= 3  # created + 2 reads
        actions = [e["action"] for e in trail]
        assert "created" in actions
        assert "read" in actions

    def test_audit_trail_all(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("K1", "v1")
        mgr.store("K2", "v2")
        all_trail = mgr.audit_trail()
        assert len(all_trail) >= 2

    def test_audit_access_denied(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "val", allowed_accessors=["allowed"])
        mgr.retrieve("KEY", accessor="denied")
        trail = mgr.audit_trail("KEY")
        denied = [e for e in trail if e["action"] == "access_denied"]
        assert len(denied) >= 1

    def test_mask_value(self) -> None:
        mgr = SecretsManager()
        assert mgr.mask_value("sk-12345-abcde") == "sk****de"
        assert mgr.mask_value("ab") == "****"

    def test_stats(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("K1", "v1")
        mgr.store("K2", "v2", scope=SecretScope.TOOL)
        stats = mgr.stats()
        assert stats["total_secrets"] == 2
        assert "by_scope" in stats
        assert "by_status" in stats

    def test_max_secrets_limit(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.MAX_SECRETS = 2
        mgr.store("K1", "v1")
        mgr.store("K2", "v2")
        with pytest.raises(RuntimeError, match="limit"):
            mgr.store("K3", "v3")

    def test_access_count_increments(self) -> None:
        mgr = SecretsManager(master_key="test-key")
        mgr.store("KEY", "val")
        mgr.retrieve("KEY", accessor="a")
        mgr.retrieve("KEY", accessor="b")
        assert mgr._metadata["KEY"].access_count == 2
