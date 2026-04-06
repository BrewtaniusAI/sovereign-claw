"""
secrets_manager — Governed Credential Storage & Rotation
=========================================================
Encrypted-at-rest credential management with scoped access.

Features:
- Obfuscated-at-rest credential storage (XOR cipher with HMAC-derived keystream;
  NOT authenticated encryption — replace with Fernet or AES-GCM for production)
- Scoped access control (per-agent, per-session, per-tool)
- Credential rotation with TTL and automatic expiry
- Audit trail for all secret access and mutations
- Secret masking in logs and outputs
- Reference-based secret injection (no plaintext in config)
- Governed secrets: all access auditable via ProofVault

Secrets are the most sensitive runtime asset.
Every access, mutation, and rotation is logged.

**Security note:** The built-in ``SimpleEncryptor`` uses XOR obfuscation with an
HMAC-SHA256-derived keystream.  This is NOT authenticated encryption and provides
no integrity protection or ciphertext authentication.  For production deployments,
replace ``SimpleEncryptor`` with ``cryptography.fernet.Fernet`` or AES-GCM.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SecretScope(str, Enum):
    """Access scope for a secret."""

    GLOBAL = "global"
    SESSION = "session"
    AGENT = "agent"
    TOOL = "tool"
    CHANNEL = "channel"


class SecretStatus(str, Enum):
    """Status of a stored secret."""

    ACTIVE = "active"
    ROTATED = "rotated"
    EXPIRED = "expired"
    REVOKED = "revoked"


class AuditAction(str, Enum):
    """Actions recorded in the audit trail."""

    CREATED = "created"
    READ = "read"
    UPDATED = "updated"
    ROTATED = "rotated"
    DELETED = "deleted"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ACCESS_DENIED = "access_denied"


@dataclass
class SecretMetadata:
    """Metadata for a stored secret (never contains the actual value)."""

    name: str
    scope: SecretScope = SecretScope.GLOBAL
    status: SecretStatus = SecretStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: float = 0.0  # 0 = no expiry
    rotation_interval_seconds: float = 0.0
    last_rotated_at: float = 0.0
    access_count: int = 0
    last_accessed_at: float = 0.0
    allowed_accessors: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    description: str = ""
    version: int = 1

    @property
    def is_expired(self) -> bool:
        if self.expires_at == 0.0:
            return False
        return time.time() > self.expires_at

    @property
    def needs_rotation(self) -> bool:
        if self.rotation_interval_seconds == 0.0:
            return False
        last = self.last_rotated_at or self.created_at
        return time.time() - last > self.rotation_interval_seconds

    @property
    def masked_name(self) -> str:
        """Return a masked version for display."""
        if len(self.name) <= 4:
            return "****"
        return self.name[:2] + "****" + self.name[-2:]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": self.scope.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "rotation_interval_seconds": self.rotation_interval_seconds,
            "access_count": self.access_count,
            "version": self.version,
            "tags": self.tags,
            "description": self.description,
            "is_expired": self.is_expired,
            "needs_rotation": self.needs_rotation,
        }


@dataclass
class AuditEntry:
    """An entry in the secrets audit trail."""

    entry_id: str = ""
    secret_name: str = ""
    action: AuditAction = AuditAction.READ
    accessor: str = ""
    timestamp: float = field(default_factory=time.time)
    success: bool = True
    details: str = ""

    def __post_init__(self) -> None:
        if not self.entry_id:
            self.entry_id = f"audit_{uuid.uuid4().hex[:10]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "secret_name": self.secret_name,
            "action": self.action.value,
            "accessor": self.accessor,
            "timestamp": self.timestamp,
            "success": self.success,
            "details": self.details,
        }


class SimpleEncryptor:
    """
    Simple symmetric obfuscation for at-rest secret storage.

    **WARNING:** This implementation uses XOR with an HMAC-SHA256-derived
    keystream.  It is NOT authenticated encryption — there is no integrity
    check, no ciphertext authentication, and no protection against bit-flipping
    attacks.  It is provided only as a zero-dependency convenience for
    development and testing.

    For production use, replace with a proper AEAD scheme such as
    ``cryptography.fernet.Fernet`` or AES-GCM from the ``cryptography`` package.
    """

    def __init__(self, master_key: str = "") -> None:
        if not master_key:
            master_key = base64.b64encode(os.urandom(32)).decode()
        self._key = master_key.encode()

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string value."""
        nonce = os.urandom(16)
        derived = hmac.new(self._key, nonce, hashlib.sha256).digest()
        data = plaintext.encode()
        encrypted = bytes(a ^ b for a, b in zip(data, self._cycle(derived, len(data))))
        combined = nonce + encrypted
        return base64.b64encode(combined).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt an encrypted string."""
        combined = base64.b64decode(ciphertext)
        nonce = combined[:16]
        encrypted = combined[16:]
        derived = hmac.new(self._key, nonce, hashlib.sha256).digest()
        decrypted = bytes(a ^ b for a, b in zip(encrypted, self._cycle(derived, len(encrypted))))
        return decrypted.decode()

    @staticmethod
    def _cycle(key: bytes, length: int) -> bytes:
        """Cycle a key to match the required length."""
        result = bytearray()
        while len(result) < length:
            result.extend(key)
        return bytes(result[:length])


class SecretsManager:
    """
    Governed credential storage with encryption, scoping, and audit.

    Usage:
        manager = SecretsManager(master_key="my-secure-key")

        # Store a secret
        manager.store("API_KEY", "sk-xxx-yyy", scope=SecretScope.GLOBAL)

        # Retrieve a secret
        value = manager.retrieve("API_KEY", accessor="orchestrator")

        # Rotate a secret
        manager.rotate("API_KEY", "sk-new-value")

        # List secrets (metadata only, no values)
        secrets = manager.list_secrets()

        # Audit trail
        trail = manager.audit_trail("API_KEY")
    """

    # Maximum secrets
    MAX_SECRETS = 1000

    # Maximum audit entries
    MAX_AUDIT_ENTRIES = 50000

    def __init__(self, master_key: str = "") -> None:
        self._encryptor = SimpleEncryptor(master_key)
        self._store: dict[str, str] = {}  # name -> encrypted value
        self._metadata: dict[str, SecretMetadata] = {}
        self._audit: list[AuditEntry] = []
        self._total_operations = 0

    def store(
        self,
        name: str,
        value: str,
        scope: SecretScope = SecretScope.GLOBAL,
        expires_at: float = 0.0,
        rotation_interval: float = 0.0,
        allowed_accessors: list[str] | None = None,
        tags: list[str] | None = None,
        description: str = "",
    ) -> SecretMetadata:
        """
        Store or update a secret.

        Args:
            name: Secret name/key.
            value: Secret value (will be encrypted).
            scope: Access scope.
            expires_at: Expiration timestamp (0 = no expiry).
            rotation_interval: Auto-rotation interval in seconds.
            allowed_accessors: List of accessor IDs allowed to read.
            tags: Categorization tags.
            description: Human-readable description.

        Returns:
            SecretMetadata for the stored secret.
        """
        if len(self._store) >= self.MAX_SECRETS and name not in self._store:
            raise RuntimeError(f"Secret limit reached ({self.MAX_SECRETS})")

        self._total_operations += 1
        now = time.time()

        # Encrypt and store
        encrypted = self._encryptor.encrypt(value)
        self._store[name] = encrypted

        # Create/update metadata
        existing = self._metadata.get(name)
        if existing:
            existing.updated_at = now
            existing.version += 1
            existing.status = SecretStatus.ACTIVE
            existing.scope = scope
            existing.expires_at = expires_at
            existing.rotation_interval_seconds = rotation_interval
            if allowed_accessors is not None:
                existing.allowed_accessors = allowed_accessors
            if tags is not None:
                existing.tags = tags
            if description:
                existing.description = description
            meta = existing
            action = AuditAction.UPDATED
        else:
            meta = SecretMetadata(
                name=name,
                scope=scope,
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
                rotation_interval_seconds=rotation_interval,
                allowed_accessors=allowed_accessors or [],
                tags=tags or [],
                description=description,
            )
            self._metadata[name] = meta
            action = AuditAction.CREATED

        self._record_audit(name, action, "system", True)
        return meta

    def retrieve(
        self,
        name: str,
        accessor: str = "",
    ) -> str | None:
        """
        Retrieve a secret value.

        Args:
            name: Secret name/key.
            accessor: ID of the entity accessing the secret.

        Returns:
            Decrypted secret value, or None if not found / access denied.
        """
        self._total_operations += 1
        meta = self._metadata.get(name)

        if not meta:
            self._record_audit(name, AuditAction.READ, accessor, False, "Not found")
            return None

        # Check expiry
        if meta.is_expired:
            meta.status = SecretStatus.EXPIRED
            self._record_audit(name, AuditAction.EXPIRED, accessor, False, "Secret expired")
            return None

        # Check revocation
        if meta.status == SecretStatus.REVOKED:
            self._record_audit(
                name,
                AuditAction.ACCESS_DENIED,
                accessor,
                False,
                "Secret has been revoked",
            )
            return None

        # Check access scope
        if meta.allowed_accessors and accessor not in meta.allowed_accessors:
            self._record_audit(
                name,
                AuditAction.ACCESS_DENIED,
                accessor,
                False,
                f"Accessor {accessor!r} not in allowed list",
            )
            return None

        # Decrypt
        encrypted = self._store.get(name)
        if not encrypted:
            self._record_audit(
                name,
                AuditAction.ACCESS_DENIED,
                accessor,
                False,
                "Encrypted value missing",
            )
            return None

        meta.access_count += 1
        meta.last_accessed_at = time.time()
        self._record_audit(name, AuditAction.READ, accessor, True)

        return self._encryptor.decrypt(encrypted)

    def rotate(
        self,
        name: str,
        new_value: str,
        accessor: str = "system",
    ) -> SecretMetadata | None:
        """
        Rotate a secret to a new value.

        Args:
            name: Secret name.
            new_value: New secret value.
            accessor: Who is performing the rotation.

        Returns:
            Updated metadata, or None if secret not found.
        """
        self._total_operations += 1
        meta = self._metadata.get(name)
        if not meta:
            return None

        encrypted = self._encryptor.encrypt(new_value)
        self._store[name] = encrypted

        now = time.time()
        meta.last_rotated_at = now
        meta.updated_at = now
        meta.version += 1
        meta.status = SecretStatus.ACTIVE

        self._record_audit(name, AuditAction.ROTATED, accessor, True)
        return meta

    def revoke(self, name: str, accessor: str = "system") -> bool:
        """Revoke a secret (mark as revoked but keep metadata)."""
        self._total_operations += 1
        meta = self._metadata.get(name)
        if not meta:
            return False

        meta.status = SecretStatus.REVOKED
        self._store.pop(name, None)
        self._record_audit(name, AuditAction.REVOKED, accessor, True)
        return True

    def delete(self, name: str, accessor: str = "system") -> bool:
        """Delete a secret entirely."""
        self._total_operations += 1
        if name not in self._metadata:
            return False

        del self._metadata[name]
        self._store.pop(name, None)
        self._record_audit(name, AuditAction.DELETED, accessor, True)
        return True

    def list_secrets(
        self,
        scope: SecretScope | None = None,
        tag: str = "",
        include_expired: bool = False,
    ) -> list[dict[str, Any]]:
        """List secrets (metadata only — no values)."""
        results = []
        for meta in self._metadata.values():
            if scope and meta.scope != scope:
                continue
            if tag and tag not in meta.tags:
                continue
            if not include_expired and meta.is_expired:
                continue
            results.append(meta.to_dict())
        return results

    def check_rotations(self) -> list[str]:
        """Check for secrets needing rotation. Returns list of names."""
        needs_rotation = []
        for meta in self._metadata.values():
            if meta.status == SecretStatus.ACTIVE and meta.needs_rotation:
                needs_rotation.append(meta.name)
        return needs_rotation

    def expire_stale(self) -> int:
        """Expire all secrets past their expiry time."""
        expired_count = 0
        for meta in self._metadata.values():
            if meta.status == SecretStatus.ACTIVE and meta.is_expired:
                meta.status = SecretStatus.EXPIRED
                self._store.pop(meta.name, None)
                self._record_audit(meta.name, AuditAction.EXPIRED, "system", True)
                expired_count += 1
        return expired_count

    def audit_trail(
        self,
        secret_name: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Get audit trail, optionally filtered by secret name."""
        entries = self._audit
        if secret_name:
            entries = [e for e in entries if e.secret_name == secret_name]
        return [e.to_dict() for e in entries[-limit:]]

    def mask_value(self, value: str) -> str:
        """Mask a secret value for safe display."""
        if len(value) <= 4:
            return "****"
        return value[:2] + "****" + value[-2:]

    def stats(self) -> dict[str, Any]:
        """Get manager statistics."""
        by_scope: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for meta in self._metadata.values():
            by_scope[meta.scope.value] = by_scope.get(meta.scope.value, 0) + 1
            by_status[meta.status.value] = by_status.get(meta.status.value, 0) + 1
        return {
            "total_secrets": len(self._metadata),
            "total_operations": self._total_operations,
            "audit_entries": len(self._audit),
            "needs_rotation": len(self.check_rotations()),
            "by_scope": by_scope,
            "by_status": by_status,
        }

    def _record_audit(
        self,
        secret_name: str,
        action: AuditAction,
        accessor: str,
        success: bool,
        details: str = "",
    ) -> None:
        """Record an audit entry."""
        entry = AuditEntry(
            secret_name=secret_name,
            action=action,
            accessor=accessor,
            success=success,
            details=details,
        )
        self._audit.append(entry)
        if len(self._audit) > self.MAX_AUDIT_ENTRIES:
            self._audit = self._audit[-self.MAX_AUDIT_ENTRIES :]
