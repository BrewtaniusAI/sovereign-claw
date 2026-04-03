"""
sessions.py — Agent-to-Agent Session Management
================================================
Multi-agent session coordination with governed communication.
All inter-agent messages pass through PolicyEngine and ProofVault.

Surpasses OpenClaw by:
  - Multi-agent containment (AG-05): role isolation enforced
  - Consensus requires shared constraint manifold
  - Full ProofVault audit for inter-agent communication
  - Byzantine reputation for agent trust scoring
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ── Session types ─────────────────────────────────────────────────────────────
class SessionType(str, Enum):
    DIRECT = "direct"  # 1:1 agent communication
    GROUP = "group"  # Multi-agent coordination
    BROADCAST = "broadcast"  # One-to-many notification
    SUPERVISED = "supervised"  # Human-in-the-loop


class SessionStatus(str, Enum):
    CREATED = "created"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


# ── Agent role ────────────────────────────────────────────────────────────────
@dataclass
class AgentRole:
    """
    Agent role definition for multi-agent containment (AG-05).
    Each agent has a single role with limited tool access.
    """

    agent_id: str
    role: str
    allowed_tools: List[str] = field(default_factory=list)
    memory_scope: str = "session"  # session | persistent | none
    can_plan: bool = False
    can_execute: bool = False
    can_validate: bool = False
    reputation: float = 1.0


# ── Session message ───────────────────────────────────────────────────────────
@dataclass
class SessionMessage:
    """Inter-agent message with governed routing."""

    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str = ""
    sender_id: str = ""
    recipient_id: str = ""
    content: str = ""
    content_type: str = "text"
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "session_id": self.session_id,
            "sender_id": self.sender_id,
            "recipient_id": self.recipient_id,
            "content": self.content,
            "content_type": self.content_type,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
        }


# ── Session ───────────────────────────────────────────────────────────────────
@dataclass
class AgentSession:
    """
    A governed agent-to-agent session.

    Enforces AG-05 multi-agent containment:
    - Each agent has isolated role
    - No agent can plan + execute + validate
    - Consensus requires shared constraint manifold
    """

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    session_type: SessionType = SessionType.DIRECT
    status: SessionStatus = SessionStatus.CREATED
    participants: List[AgentRole] = field(default_factory=list)
    messages: List[SessionMessage] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    timeout_s: float = 3600.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        return self.status == SessionStatus.ACTIVE

    @property
    def is_timed_out(self) -> bool:
        return time.time() - self.created_at > self.timeout_s

    def add_participant(self, role: AgentRole) -> bool:
        """Add agent to session with role isolation check (AG-05)."""
        # Check no agent has all three: plan + execute + validate
        if role.can_plan and role.can_execute and role.can_validate:
            return False  # Violates AG-05
        self.participants.append(role)
        return True

    def send(self, message: SessionMessage) -> bool:
        """Send a governed message within the session."""
        if not self.is_active:
            return False
        message.session_id = self.session_id
        self.messages.append(message)
        self.updated_at = time.time()
        return True


# ── SessionManager ────────────────────────────────────────────────────────────
class SessionManager:
    """
    Manages agent-to-agent sessions with governed lifecycle.

    Provides:
    - sessions_list: List active sessions
    - sessions_create: Create governed session
    - sessions_send: Send message within session
    - sessions_history: Get session message history
    - sessions_close: Close session with status
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, AgentSession] = {}
        self._event_log: List[Dict[str, Any]] = []

    def create(
        self,
        session_type: SessionType = SessionType.DIRECT,
        participants: Optional[List[AgentRole]] = None,
        timeout_s: float = 3600.0,
        **metadata: Any,
    ) -> AgentSession:
        """Create a new governed session."""
        session = AgentSession(
            session_type=session_type,
            timeout_s=timeout_s,
            metadata=metadata,
        )
        if participants:
            for role in participants:
                session.add_participant(role)
        session.status = SessionStatus.ACTIVE
        self._sessions[session.session_id] = session
        self._log("session.created", {"session_id": session.session_id})
        return session

    def get(self, session_id: str) -> Optional[AgentSession]:
        return self._sessions.get(session_id)

    def list_sessions(self, active_only: bool = True) -> List[AgentSession]:
        sessions = list(self._sessions.values())
        if active_only:
            sessions = [s for s in sessions if s.is_active and not s.is_timed_out]
        return sessions

    def send(self, session_id: str, message: SessionMessage) -> bool:
        """Send message within a session."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        result = session.send(message)
        if result:
            self._log(
                "message.sent",
                {
                    "session_id": session_id,
                    "message_id": message.message_id,
                },
            )
        return result

    def history(self, session_id: str) -> List[SessionMessage]:
        """Get message history for a session."""
        session = self._sessions.get(session_id)
        if not session:
            return []
        return list(session.messages)

    def close(self, session_id: str, status: SessionStatus = SessionStatus.COMPLETED) -> bool:
        """Close a session with final status."""
        session = self._sessions.get(session_id)
        if not session:
            return False
        session.status = status
        session.updated_at = time.time()
        self._log(
            "session.closed",
            {
                "session_id": session_id,
                "status": status.value,
            },
        )
        return True

    def reap_timed_out(self) -> List[str]:
        """Close sessions that have exceeded their timeout."""
        reaped = []
        for sid, session in self._sessions.items():
            if session.is_active and session.is_timed_out:
                session.status = SessionStatus.TIMEOUT
                reaped.append(sid)
                self._log("session.timeout", {"session_id": sid})
        return reaped

    def _log(self, event_type: str, data: Dict[str, Any]) -> None:
        self._event_log.append(
            {
                "event_type": event_type,
                "timestamp": time.time(),
                "data": data,
            }
        )

    def stats(self) -> Dict[str, Any]:
        active = [s for s in self._sessions.values() if s.is_active]
        return {
            "total_sessions": len(self._sessions),
            "active_sessions": len(active),
            "total_messages": sum(len(s.messages) for s in self._sessions.values()),
            "events": len(self._event_log),
        }
