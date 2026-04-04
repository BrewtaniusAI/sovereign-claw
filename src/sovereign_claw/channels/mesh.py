"""
mesh.py — Channel Interface Mesh
=================================
Cross-channel identity linking, session continuity, and per-channel
policy overrides. Turns the 8-channel connector layer into a governed
"sovereign interface mesh" with unified identity management.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


ChannelType = Literal[
    "discord",
    "slack",
    "telegram",
    "whatsapp",
    "webchat",
    "irc",
    "matrix",
    "signal",
]


@dataclass
class ChannelIdentity:
    """Cross-channel identity record linking a user across channels."""

    identity_id: str
    display_name: str
    channel_accounts: Dict[ChannelType, str] = field(default_factory=dict)
    created_at: float = 0.0
    last_seen: float = 0.0
    trust_score: float = 1.0

    def __post_init__(self) -> None:
        if self.created_at == 0.0:
            self.created_at = time.time()
        if self.last_seen == 0.0:
            self.last_seen = self.created_at


@dataclass
class MeshSession:
    """A session that spans across channels with continuity."""

    session_id: str
    identity_id: str
    active_channel: ChannelType
    context: Dict[str, Any] = field(default_factory=dict)
    history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = 0.0
    last_active: float = 0.0

    def __post_init__(self) -> None:
        if self.created_at == 0.0:
            self.created_at = time.time()
        if self.last_active == 0.0:
            self.last_active = self.created_at


@dataclass
class ChannelPolicy:
    """Per-channel policy override."""

    channel: ChannelType
    policy_profile: str = "balanced"
    max_message_length: int = 4096
    allow_file_uploads: bool = True
    rate_limit_per_minute: int = 60
    require_identity: bool = False


class ChannelMesh:
    """
    Sovereign Interface Mesh — one brain, many governed interfaces.

    Features:
      - Cross-channel identity linking
      - Session continuity across channel switches
      - Per-channel policy overrides
      - Unified message routing
    """

    def __init__(self) -> None:
        self._identities: Dict[str, ChannelIdentity] = {}
        self._sessions: Dict[str, MeshSession] = {}
        self._channel_policies: Dict[ChannelType, ChannelPolicy] = {}
        self._channel_to_identity: Dict[str, str] = {}  # "channel:account" -> identity_id

    def register_identity(
        self,
        display_name: str,
        channel_accounts: Optional[Dict[ChannelType, str]] = None,
    ) -> ChannelIdentity:
        """Register a new cross-channel identity."""
        identity_id = f"id_{uuid.uuid4().hex[:12]}"
        identity = ChannelIdentity(
            identity_id=identity_id,
            display_name=display_name,
            channel_accounts=channel_accounts or {},
        )
        self._identities[identity_id] = identity

        # Index channel accounts
        for channel, account in identity.channel_accounts.items():
            key = f"{channel}:{account}"
            self._channel_to_identity[key] = identity_id

        return identity

    def link_account(
        self,
        identity_id: str,
        channel: ChannelType,
        account: str,
    ) -> bool:
        """Link a new channel account to an existing identity."""
        identity = self._identities.get(identity_id)
        if not identity:
            return False

        identity.channel_accounts[channel] = account
        key = f"{channel}:{account}"
        self._channel_to_identity[key] = identity_id
        return True

    def resolve_identity(
        self,
        channel: ChannelType,
        account: str,
    ) -> Optional[ChannelIdentity]:
        """Resolve a channel+account to a cross-channel identity."""
        key = f"{channel}:{account}"
        identity_id = self._channel_to_identity.get(key)
        if identity_id:
            identity = self._identities.get(identity_id)
            if identity:
                identity.last_seen = time.time()
                return identity
        return None

    def create_session(
        self,
        identity_id: str,
        channel: ChannelType,
        context: Optional[Dict[str, Any]] = None,
    ) -> MeshSession:
        """Create a new mesh session for an identity."""
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        session = MeshSession(
            session_id=session_id,
            identity_id=identity_id,
            active_channel=channel,
            context=context or {},
        )
        self._sessions[session_id] = session
        return session

    def switch_channel(
        self,
        session_id: str,
        new_channel: ChannelType,
    ) -> bool:
        """Switch a session to a different channel (session continuity)."""
        session = self._sessions.get(session_id)
        if not session:
            return False

        session.active_channel = new_channel
        session.last_active = time.time()
        return True

    def get_session(self, session_id: str) -> Optional[MeshSession]:
        """Get a mesh session by ID."""
        return self._sessions.get(session_id)

    def set_channel_policy(self, policy: ChannelPolicy) -> None:
        """Set or update a per-channel policy override."""
        self._channel_policies[policy.channel] = policy

    def get_channel_policy(self, channel: ChannelType) -> ChannelPolicy:
        """Get the policy for a specific channel (defaults if not set)."""
        return self._channel_policies.get(
            channel,
            ChannelPolicy(channel=channel),
        )

    def add_message(
        self,
        session_id: str,
        message: Dict[str, Any],
    ) -> bool:
        """Add a message to a session's history."""
        session = self._sessions.get(session_id)
        if not session:
            return False

        session.history.append(
            {
                **message,
                "channel": session.active_channel,
                "timestamp": time.time(),
            }
        )
        session.last_active = time.time()
        return True

    def list_identities(self) -> List[ChannelIdentity]:
        """List all registered identities."""
        return list(self._identities.values())

    def list_sessions(
        self,
        identity_id: Optional[str] = None,
    ) -> List[MeshSession]:
        """List sessions, optionally filtered by identity."""
        sessions = list(self._sessions.values())
        if identity_id:
            sessions = [s for s in sessions if s.identity_id == identity_id]
        return sessions
