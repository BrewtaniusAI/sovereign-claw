"""
chat_commands — In-Channel Command System
==========================================
Governed command framework for interactive agent control.

Features:
- Command registration with help text and permission requirements
- Built-in commands: /status, /new, /reset, /compact, /think, /verbose, /usage
- Custom command support with handler functions
- Command parsing with arguments and flags
- Per-channel command enablement
- Command rate limiting
- Governed commands: all command executions auditable

Commands give users direct control over agent behavior
within any channel interface.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class CommandPermission(str, Enum):
    """Permission levels for commands."""

    PUBLIC = "public"  # Anyone can use
    OPERATOR = "operator"  # Requires operator role
    ADMIN = "admin"  # Requires admin role


class CommandStatus(str, Enum):
    """Status of a command execution."""

    SUCCESS = "success"
    ERROR = "error"
    DENIED = "denied"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    DISABLED = "disabled"


@dataclass
class CommandArg:
    """A command argument definition."""

    name: str
    description: str = ""
    required: bool = False
    default: str = ""
    choices: list[str] = field(default_factory=list)


@dataclass
class ParsedCommand:
    """Result of parsing a command string."""

    name: str = ""
    args: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)
    raw: str = ""
    valid: bool = True
    error: str = ""

    def get_arg(self, index: int, default: str = "") -> str:
        """Get positional argument by index."""
        if index < len(self.args):
            return self.args[index]
        return default

    def get_flag(self, name: str, default: str = "") -> str:
        """Get a flag value."""
        return self.flags.get(name, default)

    def has_flag(self, name: str) -> bool:
        """Check if a flag is present."""
        return name in self.flags


@dataclass
class CommandResult:
    """Result of a command execution."""

    status: CommandStatus = CommandStatus.SUCCESS
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    command_name: str = ""
    execution_time_ms: float = 0.0
    channel: str = ""

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status.value,
            "command": self.command_name,
            "message": self.message,
        }
        if self.data:
            result["data"] = self.data
        if self.execution_time_ms:
            result["execution_time_ms"] = round(self.execution_time_ms, 2)
        return result


# Type for command handlers
CommandHandler = Callable[[ParsedCommand, dict[str, Any]], CommandResult]


@dataclass
class CommandDefinition:
    """Definition of a chat command."""

    name: str
    description: str
    handler: CommandHandler
    permission: CommandPermission = CommandPermission.PUBLIC
    args: list[CommandArg] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    enabled: bool = True
    hidden: bool = False
    cooldown_seconds: float = 0.0
    category: str = "general"

    @property
    def usage(self) -> str:
        """Generate usage string."""
        parts = [f"/{self.name}"]
        for arg in self.args:
            if arg.required:
                parts.append(f"<{arg.name}>")
            else:
                parts.append(f"[{arg.name}]")
        return " ".join(parts)


def parse_command(text: str, prefix: str = "/") -> ParsedCommand:
    """
    Parse a command string into components.

    Examples:
        /status                    -> name="status", args=[], flags={}
        /think deep                -> name="think", args=["deep"], flags={}
        /compact --strategy prune  -> name="compact", args=[], flags={"strategy": "prune"}
        /verbose on                -> name="verbose", args=["on"], flags={}
    """
    text = text.strip()
    if not text.startswith(prefix):
        return ParsedCommand(raw=text, valid=False, error="Missing command prefix")

    # Remove prefix
    text = text[len(prefix) :]
    if not text:
        return ParsedCommand(raw=text, valid=False, error="Empty command")

    parts = text.split()
    name = parts[0].lower()
    args: list[str] = []
    flags: dict[str, str] = {}

    i = 1
    while i < len(parts):
        part = parts[i]
        if part.startswith("--"):
            flag_name = part[2:]
            if i + 1 < len(parts) and not parts[i + 1].startswith("--"):
                flags[flag_name] = parts[i + 1]
                i += 2
            else:
                flags[flag_name] = "true"
                i += 1
        else:
            args.append(part)
            i += 1

    return ParsedCommand(
        name=name,
        args=args,
        flags=flags,
        raw=text,
    )


# ── Built-in command handlers ────────────────────────────────────────────────


def _handle_status(cmd: ParsedCommand, ctx: dict[str, Any]) -> CommandResult:
    """Handle /status — show agent status."""
    runtime = ctx.get("runtime", {})
    return CommandResult(
        status=CommandStatus.SUCCESS,
        message="Agent status",
        data={
            "state": runtime.get("state", "running"),
            "drift": runtime.get("drift", 0.0),
            "uptime_seconds": runtime.get("uptime_seconds", 0),
            "active_sessions": runtime.get("active_sessions", 0),
            "version": runtime.get("version", "unknown"),
        },
    )


def _handle_new(cmd: ParsedCommand, ctx: dict[str, Any]) -> CommandResult:
    """Handle /new — start a new session."""
    session_name = cmd.get_arg(0, "default")
    return CommandResult(
        status=CommandStatus.SUCCESS,
        message=f"New session started: {session_name}",
        data={"session_name": session_name},
    )


def _handle_reset(cmd: ParsedCommand, ctx: dict[str, Any]) -> CommandResult:
    """Handle /reset — reset current session context."""
    keep_system = not cmd.has_flag("full")
    return CommandResult(
        status=CommandStatus.SUCCESS,
        message="Session context reset" + (" (full)" if not keep_system else ""),
        data={"keep_system": keep_system},
    )


def _handle_compact(cmd: ParsedCommand, ctx: dict[str, Any]) -> CommandResult:
    """Handle /compact — trigger context compaction."""
    strategy = cmd.get_flag("strategy", "sliding_window")
    return CommandResult(
        status=CommandStatus.SUCCESS,
        message=f"Context compacted using {strategy}",
        data={"strategy": strategy},
    )


def _handle_think(cmd: ParsedCommand, ctx: dict[str, Any]) -> CommandResult:
    """Handle /think — set thinking mode (deep/fast)."""
    mode = cmd.get_arg(0, "deep")
    if mode not in ("deep", "fast", "balanced"):
        return CommandResult(
            status=CommandStatus.ERROR,
            message=f"Invalid think mode: {mode}. Use: deep, fast, balanced",
        )
    return CommandResult(
        status=CommandStatus.SUCCESS,
        message=f"Thinking mode set to: {mode}",
        data={"mode": mode},
    )


def _handle_verbose(cmd: ParsedCommand, ctx: dict[str, Any]) -> CommandResult:
    """Handle /verbose — toggle verbose output."""
    toggle = cmd.get_arg(0, "toggle")
    if toggle == "toggle":
        current = ctx.get("verbose", False)
        new_state = not current
    elif toggle in ("on", "true", "1"):
        new_state = True
    else:
        new_state = False
    return CommandResult(
        status=CommandStatus.SUCCESS,
        message=f"Verbose mode: {'on' if new_state else 'off'}",
        data={"verbose": new_state},
    )


def _handle_usage(cmd: ParsedCommand, ctx: dict[str, Any]) -> CommandResult:
    """Handle /usage — show token/cost usage."""
    usage = ctx.get("usage", {})
    return CommandResult(
        status=CommandStatus.SUCCESS,
        message="Usage report",
        data={
            "total_tokens": usage.get("total_tokens", 0),
            "total_cost": usage.get("total_cost", 0.0),
            "session_tokens": usage.get("session_tokens", 0),
            "session_cost": usage.get("session_cost", 0.0),
        },
    )


def _handle_help(cmd: ParsedCommand, ctx: dict[str, Any]) -> CommandResult:
    """Handle /help — list available commands."""
    commands = ctx.get("commands", {})
    lines = []
    for name, defn in sorted(commands.items()):
        if not defn.get("hidden", False):
            desc = defn.get("description", "")
            usage = defn.get("usage", f"/{name}")
            lines.append(f"{usage} — {desc}")
    return CommandResult(
        status=CommandStatus.SUCCESS,
        message="Available commands:\n" + "\n".join(lines) if lines else "No commands",
        data={"command_count": len(lines)},
    )


class ChatCommandRegistry:
    """
    Chat command registry and executor.

    Usage:
        registry = ChatCommandRegistry()

        # Built-in commands are pre-registered.
        # Execute a command:
        result = registry.execute("/status", channel="discord")

        # Register custom command:
        registry.register(CommandDefinition(
            name="deploy",
            description="Deploy the current build",
            handler=my_deploy_handler,
            permission=CommandPermission.ADMIN,
        ))

        # List commands:
        cmds = registry.list_commands()
    """

    def __init__(self, prefix: str = "/") -> None:
        self._prefix = prefix
        self._commands: dict[str, CommandDefinition] = {}
        self._aliases: dict[str, str] = {}
        self._disabled_channels: set[str] = set()
        self._cooldowns: dict[str, float] = {}  # "cmd:channel" -> last_exec timestamp
        self._total_executions = 0
        self._total_errors = 0
        self._execution_counts: dict[str, int] = {}

        # Register built-in commands
        self._register_builtins()

    def register(self, definition: CommandDefinition) -> None:
        """Register a command."""
        self._commands[definition.name] = definition
        for alias in definition.aliases:
            self._aliases[alias] = definition.name

    def unregister(self, name: str) -> bool:
        """Unregister a command."""
        defn = self._commands.pop(name, None)
        if defn:
            for alias in defn.aliases:
                self._aliases.pop(alias, None)
            return True
        return False

    def execute(
        self,
        text: str,
        channel: str = "",
        user_role: CommandPermission = CommandPermission.PUBLIC,
        context: dict[str, Any] | None = None,
    ) -> CommandResult:
        """
        Parse and execute a command.

        Args:
            text: Raw command string (e.g., "/status").
            channel: Channel where command was issued.
            user_role: Permission level of the user.
            context: Runtime context passed to handler.

        Returns:
            CommandResult with execution status.
        """
        start = time.time()
        self._total_executions += 1

        # Parse
        parsed = parse_command(text, self._prefix)
        if not parsed.valid:
            self._total_errors += 1
            return CommandResult(
                status=CommandStatus.ERROR,
                message=parsed.error,
                command_name=parsed.name,
            )

        # Resolve alias
        cmd_name = self._aliases.get(parsed.name, parsed.name)

        # Find command
        defn = self._commands.get(cmd_name)
        if not defn:
            return CommandResult(
                status=CommandStatus.NOT_FOUND,
                message=f"Unknown command: /{parsed.name}",
                command_name=parsed.name,
            )

        # Check enabled
        if not defn.enabled:
            return CommandResult(
                status=CommandStatus.DISABLED,
                message=f"Command /{cmd_name} is disabled",
                command_name=cmd_name,
            )

        # Check channel
        if channel and channel in self._disabled_channels:
            return CommandResult(
                status=CommandStatus.DISABLED,
                message=f"Commands disabled in channel: {channel}",
                command_name=cmd_name,
                channel=channel,
            )

        # Check permission
        if not self._check_permission(user_role, defn.permission):
            return CommandResult(
                status=CommandStatus.DENIED,
                message=f"Permission denied: /{cmd_name} requires {defn.permission.value}",
                command_name=cmd_name,
                channel=channel,
            )

        # Check cooldown
        if defn.cooldown_seconds > 0:
            cooldown_key = f"{cmd_name}:{channel}"
            last_exec = self._cooldowns.get(cooldown_key, 0.0)
            if time.time() - last_exec < defn.cooldown_seconds:
                return CommandResult(
                    status=CommandStatus.RATE_LIMITED,
                    message=f"Command on cooldown ({defn.cooldown_seconds}s)",
                    command_name=cmd_name,
                    channel=channel,
                )
            self._cooldowns[cooldown_key] = time.time()

        # Inject command list into context for /help
        ctx = context or {}
        ctx["commands"] = {
            name: {
                "description": d.description,
                "usage": d.usage,
                "hidden": d.hidden,
            }
            for name, d in self._commands.items()
        }

        # Execute
        try:
            result = defn.handler(parsed, ctx)
            result.command_name = cmd_name
            result.channel = channel
            result.execution_time_ms = (time.time() - start) * 1000
        except Exception as exc:
            self._total_errors += 1
            result = CommandResult(
                status=CommandStatus.ERROR,
                message=f"Command error: {exc}",
                command_name=cmd_name,
                channel=channel,
                execution_time_ms=(time.time() - start) * 1000,
            )

        self._execution_counts[cmd_name] = self._execution_counts.get(cmd_name, 0) + 1
        return result

    def list_commands(
        self,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        """List all registered commands."""
        results = []
        for name, defn in sorted(self._commands.items()):
            if defn.hidden and not include_hidden:
                continue
            results.append(
                {
                    "name": name,
                    "description": defn.description,
                    "usage": defn.usage,
                    "permission": defn.permission.value,
                    "category": defn.category,
                    "aliases": defn.aliases,
                    "enabled": defn.enabled,
                }
            )
        return results

    def disable_channel(self, channel: str) -> None:
        """Disable commands in a channel."""
        self._disabled_channels.add(channel)

    def enable_channel(self, channel: str) -> None:
        """Enable commands in a channel."""
        self._disabled_channels.discard(channel)

    def stats(self) -> dict[str, Any]:
        """Get command statistics."""
        return {
            "total_commands": len(self._commands),
            "total_executions": self._total_executions,
            "total_errors": self._total_errors,
            "execution_counts": dict(self._execution_counts),
            "disabled_channels": list(self._disabled_channels),
        }

    def _check_permission(
        self,
        user_role: CommandPermission,
        required: CommandPermission,
    ) -> bool:
        """Check if user role meets permission requirement."""
        role_hierarchy = {
            CommandPermission.PUBLIC: 0,
            CommandPermission.OPERATOR: 1,
            CommandPermission.ADMIN: 2,
        }
        return role_hierarchy.get(user_role, 0) >= role_hierarchy.get(required, 0)

    def _register_builtins(self) -> None:
        """Register built-in commands."""
        builtins = [
            CommandDefinition(
                name="status",
                description="Show agent status and drift",
                handler=_handle_status,
                aliases=["s", "info"],
                category="system",
            ),
            CommandDefinition(
                name="new",
                description="Start a new session",
                handler=_handle_new,
                args=[CommandArg(name="name", description="Session name")],
                category="session",
            ),
            CommandDefinition(
                name="reset",
                description="Reset current session context",
                handler=_handle_reset,
                aliases=["clear"],
                category="session",
            ),
            CommandDefinition(
                name="compact",
                description="Trigger context compaction",
                handler=_handle_compact,
                category="context",
            ),
            CommandDefinition(
                name="think",
                description="Set thinking mode (deep/fast/balanced)",
                handler=_handle_think,
                args=[
                    CommandArg(
                        name="mode",
                        description="Thinking mode",
                        choices=["deep", "fast", "balanced"],
                    )
                ],
                category="execution",
            ),
            CommandDefinition(
                name="verbose",
                description="Toggle verbose output",
                handler=_handle_verbose,
                args=[CommandArg(name="toggle", description="on/off/toggle")],
                aliases=["v"],
                category="output",
            ),
            CommandDefinition(
                name="usage",
                description="Show token and cost usage",
                handler=_handle_usage,
                category="monitoring",
            ),
            CommandDefinition(
                name="help",
                description="List available commands",
                handler=_handle_help,
                aliases=["h", "?"],
                category="system",
            ),
        ]
        for defn in builtins:
            self.register(defn)
