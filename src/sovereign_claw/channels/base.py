"""
channels.base — Abstract Channel Protocol
==========================================
Defines the governed channel interface that all messaging connectors
must implement. Channels are Repository-Bound Agents (AG-01) with
explicit specifications, test suites, and evaluation harnesses.
"""

from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol


# ── Message types ─────────────────────────────────────────────────────────────
class MessageDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class ContentType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    LOCATION = "location"
    REACTION = "reaction"
    CANVAS = "canvas"
    SYSTEM = "system"


@dataclass
class ChannelUser:
    """Represents a user on any messaging channel."""

    user_id: str
    display_name: str = ""
    channel: str = ""
    is_bot: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChannelMessage:
    """
    Universal message format across all channels.
    Every message gets a trace_id for ProofVault logging.
    """

    message_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    channel: str = ""
    direction: MessageDirection = MessageDirection.INBOUND
    content_type: ContentType = ContentType.TEXT
    text: str = ""
    sender: Optional[ChannelUser] = None
    recipient: Optional[ChannelUser] = None
    reply_to: str = ""
    thread_id: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "trace_id": self.trace_id,
            "channel": self.channel,
            "direction": self.direction.value,
            "content_type": self.content_type.value,
            "text": self.text,
            "sender": self.sender.__dict__ if self.sender else None,
            "recipient": self.recipient.__dict__ if self.recipient else None,
            "reply_to": self.reply_to,
            "thread_id": self.thread_id,
            "attachments": self.attachments,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }


# ── Channel status ────────────────────────────────────────────────────────────
class ChannelStatus(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    AUTHENTICATED = "authenticated"
    ERROR = "error"


# ── Message handler protocol ─────────────────────────────────────────────────
class MessageHandler(Protocol):
    """Protocol for message processing callbacks."""

    async def handle(self, message: ChannelMessage) -> Optional[ChannelMessage]: ...


# ── Abstract Channel ──────────────────────────────────────────────────────────
class Channel(ABC):
    """
    Abstract base for all messaging channel connectors.

    Every channel must implement:
    - connect(): Establish connection to the platform
    - disconnect(): Clean shutdown
    - send(): Send a governed message
    - on_message(): Register message handler

    Channels are governed agents (AG-01):
    - Bounded execution time
    - Explicit tool declarations
    - ProofVault audit trail
    """

    def __init__(self, name: str, **config: Any) -> None:
        self.name = name
        self.config = config
        self.status = ChannelStatus.DISCONNECTED
        self._handlers: List[MessageHandler] = []
        self._message_log: List[ChannelMessage] = []

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection to the messaging platform."""
        ...

    @abstractmethod
    async def disconnect(self) -> bool:
        """Cleanly disconnect from the platform."""
        ...

    @abstractmethod
    async def send(self, message: ChannelMessage) -> bool:
        """Send a message through this channel."""
        ...

    def on_message(self, handler: MessageHandler) -> None:
        """Register a message handler."""
        self._handlers.append(handler)

    async def _dispatch(self, message: ChannelMessage) -> None:
        """Dispatch inbound message to all handlers."""
        message.channel = self.name
        self._message_log.append(message)
        for handler in self._handlers:
            await handler.handle(message)

    @property
    def message_count(self) -> int:
        return len(self._message_log)

    def get_stats(self) -> Dict[str, Any]:
        return {
            "channel": self.name,
            "status": self.status.value,
            "messages_processed": self.message_count,
            "handlers_registered": len(self._handlers),
        }
