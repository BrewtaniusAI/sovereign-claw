"""
channels.connectors — Concrete Channel Implementations
=======================================================
Discord, Slack, Telegram, WhatsApp, WebChat, IRC, Matrix, Signal connectors.
Each wraps its platform's API behind the governed Channel interface.
"""

from __future__ import annotations

from typing import Any, Dict

from .base import (
    Channel,
    ChannelMessage,
    ChannelStatus,
    MessageDirection,
)


# ── Discord ───────────────────────────────────────────────────────────────────
class DiscordChannel(Channel):
    """
    Discord connector via Bot API.
    Supports guilds, DMs, threads, reactions, and voice channels.
    """

    def __init__(self, token: str = "", **config: Any) -> None:
        super().__init__("discord", **config)
        self.token = token
        self._ws: Any = None
        self._heartbeat_interval: float = 41.25

    async def connect(self) -> bool:
        if not self.token:
            self.status = ChannelStatus.ERROR
            return False
        self.status = ChannelStatus.CONNECTING
        # In production, connect to wss://gateway.discord.gg/?v=10&encoding=json
        self.status = ChannelStatus.CONNECTED
        return True

    async def disconnect(self) -> bool:
        self.status = ChannelStatus.DISCONNECTED
        return True

    async def send(self, message: ChannelMessage) -> bool:
        if self.status != ChannelStatus.CONNECTED:
            return False
        message.direction = MessageDirection.OUTBOUND
        message.channel = self.name
        self._message_log.append(message)
        # In production: POST /api/v10/channels/{channel_id}/messages
        return True


# ── Slack ─────────────────────────────────────────────────────────────────────
class SlackChannel(Channel):
    """
    Slack connector via Bot/App API.
    Supports channels, DMs, threads, blocks, and reactions.
    """

    def __init__(self, token: str = "", app_token: str = "", **config: Any) -> None:
        super().__init__("slack", **config)
        self.token = token
        self.app_token = app_token

    async def connect(self) -> bool:
        if not self.token:
            self.status = ChannelStatus.ERROR
            return False
        self.status = ChannelStatus.CONNECTED
        return True

    async def disconnect(self) -> bool:
        self.status = ChannelStatus.DISCONNECTED
        return True

    async def send(self, message: ChannelMessage) -> bool:
        if self.status != ChannelStatus.CONNECTED:
            return False
        message.direction = MessageDirection.OUTBOUND
        message.channel = self.name
        self._message_log.append(message)
        # In production: POST https://slack.com/api/chat.postMessage
        return True


# ── Telegram ──────────────────────────────────────────────────────────────────
class TelegramChannel(Channel):
    """
    Telegram connector via Bot API.
    Supports groups, channels, inline keyboards, and media.
    """

    def __init__(self, token: str = "", **config: Any) -> None:
        super().__init__("telegram", **config)
        self.token = token
        self._offset: int = 0

    async def connect(self) -> bool:
        if not self.token:
            self.status = ChannelStatus.ERROR
            return False
        self.status = ChannelStatus.CONNECTED
        return True

    async def disconnect(self) -> bool:
        self.status = ChannelStatus.DISCONNECTED
        return True

    async def send(self, message: ChannelMessage) -> bool:
        if self.status != ChannelStatus.CONNECTED:
            return False
        message.direction = MessageDirection.OUTBOUND
        message.channel = self.name
        self._message_log.append(message)
        # In production: POST https://api.telegram.org/bot{token}/sendMessage
        return True


# ── WhatsApp ──────────────────────────────────────────────────────────────────
class WhatsAppChannel(Channel):
    """
    WhatsApp connector via Cloud API.
    Supports text, media, templates, and interactive messages.
    """

    def __init__(
        self,
        phone_number_id: str = "",
        access_token: str = "",
        **config: Any,
    ) -> None:
        super().__init__("whatsapp", **config)
        self.phone_number_id = phone_number_id
        self.access_token = access_token

    async def connect(self) -> bool:
        if not self.access_token:
            self.status = ChannelStatus.ERROR
            return False
        self.status = ChannelStatus.CONNECTED
        return True

    async def disconnect(self) -> bool:
        self.status = ChannelStatus.DISCONNECTED
        return True

    async def send(self, message: ChannelMessage) -> bool:
        if self.status != ChannelStatus.CONNECTED:
            return False
        message.direction = MessageDirection.OUTBOUND
        message.channel = self.name
        self._message_log.append(message)
        # In production: POST https://graph.facebook.com/v18.0/{phone_number_id}/messages
        return True


# ── WebChat ───────────────────────────────────────────────────────────────────
class WebChatChannel(Channel):
    """
    Built-in WebSocket-based web chat.
    Embeddable in any web application via the operator console.
    """

    def __init__(self, **config: Any) -> None:
        super().__init__("webchat", **config)
        self._connections: Dict[str, Any] = {}

    async def connect(self) -> bool:
        self.status = ChannelStatus.CONNECTED
        return True

    async def disconnect(self) -> bool:
        self._connections.clear()
        self.status = ChannelStatus.DISCONNECTED
        return True

    async def send(self, message: ChannelMessage) -> bool:
        message.direction = MessageDirection.OUTBOUND
        message.channel = self.name
        self._message_log.append(message)
        return True


# ── IRC ───────────────────────────────────────────────────────────────────────
class IRCChannel(Channel):
    """
    IRC connector supporting standard IRC protocol.
    Supports channels, DMs, and basic commands.
    """

    def __init__(
        self,
        server: str = "",
        port: int = 6667,
        nick: str = "sovereign",
        **config: Any,
    ) -> None:
        super().__init__("irc", **config)
        self.server = server
        self.port = port
        self.nick = nick

    async def connect(self) -> bool:
        if not self.server:
            self.status = ChannelStatus.ERROR
            return False
        self.status = ChannelStatus.CONNECTED
        return True

    async def disconnect(self) -> bool:
        self.status = ChannelStatus.DISCONNECTED
        return True

    async def send(self, message: ChannelMessage) -> bool:
        if self.status != ChannelStatus.CONNECTED:
            return False
        message.direction = MessageDirection.OUTBOUND
        message.channel = self.name
        self._message_log.append(message)
        return True


# ── Matrix ────────────────────────────────────────────────────────────────────
class MatrixChannel(Channel):
    """
    Matrix connector via Client-Server API.
    Supports rooms, E2EE, threads, and federation.
    """

    def __init__(
        self,
        homeserver: str = "",
        access_token: str = "",
        **config: Any,
    ) -> None:
        super().__init__("matrix", **config)
        self.homeserver = homeserver
        self.access_token = access_token

    async def connect(self) -> bool:
        if not self.homeserver or not self.access_token:
            self.status = ChannelStatus.ERROR
            return False
        self.status = ChannelStatus.CONNECTED
        return True

    async def disconnect(self) -> bool:
        self.status = ChannelStatus.DISCONNECTED
        return True

    async def send(self, message: ChannelMessage) -> bool:
        if self.status != ChannelStatus.CONNECTED:
            return False
        message.direction = MessageDirection.OUTBOUND
        message.channel = self.name
        self._message_log.append(message)
        # In production: PUT /_matrix/client/v3/rooms/{roomId}/send/{eventType}/{txnId}
        return True


# ── Signal ────────────────────────────────────────────────────────────────────
class SignalChannel(Channel):
    """
    Signal connector via Signal CLI or signald.
    Supports E2EE messaging, groups, and media.
    """

    def __init__(self, phone_number: str = "", **config: Any) -> None:
        super().__init__("signal", **config)
        self.phone_number = phone_number

    async def connect(self) -> bool:
        if not self.phone_number:
            self.status = ChannelStatus.ERROR
            return False
        self.status = ChannelStatus.CONNECTED
        return True

    async def disconnect(self) -> bool:
        self.status = ChannelStatus.DISCONNECTED
        return True

    async def send(self, message: ChannelMessage) -> bool:
        if self.status != ChannelStatus.CONNECTED:
            return False
        message.direction = MessageDirection.OUTBOUND
        message.channel = self.name
        self._message_log.append(message)
        return True


# ── Channel registry ──────────────────────────────────────────────────────────
CHANNEL_REGISTRY: Dict[str, type] = {
    "discord": DiscordChannel,
    "slack": SlackChannel,
    "telegram": TelegramChannel,
    "whatsapp": WhatsAppChannel,
    "webchat": WebChatChannel,
    "irc": IRCChannel,
    "matrix": MatrixChannel,
    "signal": SignalChannel,
}


def create_channel(name: str, **config: Any) -> Channel:
    """Factory to create a channel by name."""
    cls = CHANNEL_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown channel: {name}. Available: {list(CHANNEL_REGISTRY.keys())}")
    return cls(**config)
