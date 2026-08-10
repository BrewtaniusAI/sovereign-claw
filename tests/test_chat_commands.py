"""Tests for sovereign_claw.chat_commands."""

from __future__ import annotations

from sovereign_claw.chat_commands import (
    ChatCommandRegistry,
    CommandDefinition,
    CommandPermission,
    CommandResult,
    CommandStatus,
    ParsedCommand,
    parse_command,
)


# ── parse_command ────────────────────────────────────────────────────────────


class TestParseCommand:
    def test_simple_command(self) -> None:
        parsed = parse_command("/status")
        assert parsed.name == "status"
        assert parsed.valid
        assert len(parsed.args) == 0

    def test_command_with_arg(self) -> None:
        parsed = parse_command("/think deep")
        assert parsed.name == "think"
        assert parsed.args == ["deep"]

    def test_command_with_flag(self) -> None:
        parsed = parse_command("/compact --strategy prune")
        assert parsed.name == "compact"
        assert parsed.flags["strategy"] == "prune"

    def test_command_with_boolean_flag(self) -> None:
        parsed = parse_command("/reset --full")
        assert parsed.name == "reset"
        assert parsed.flags["full"] == "true"

    def test_command_mixed_args_flags(self) -> None:
        parsed = parse_command("/new session1 --mode strict")
        assert parsed.name == "new"
        assert parsed.args == ["session1"]
        assert parsed.flags["mode"] == "strict"

    def test_missing_prefix(self) -> None:
        parsed = parse_command("status")
        assert not parsed.valid
        assert "prefix" in parsed.error.lower()

    def test_empty_command(self) -> None:
        parsed = parse_command("/")
        assert not parsed.valid

    def test_custom_prefix(self) -> None:
        parsed = parse_command("!status", prefix="!")
        assert parsed.name == "status"
        assert parsed.valid

    def test_case_insensitive(self) -> None:
        parsed = parse_command("/STATUS")
        assert parsed.name == "status"

    def test_get_arg(self) -> None:
        parsed = parse_command("/think deep")
        assert parsed.get_arg(0) == "deep"
        assert parsed.get_arg(1, "fallback") == "fallback"

    def test_get_flag(self) -> None:
        parsed = parse_command("/compact --strategy prune")
        assert parsed.get_flag("strategy") == "prune"
        assert parsed.get_flag("missing", "default") == "default"

    def test_has_flag(self) -> None:
        parsed = parse_command("/reset --full")
        assert parsed.has_flag("full")
        assert not parsed.has_flag("partial")


# ── CommandResult ────────────────────────────────────────────────────────────


class TestCommandResult:
    def test_success(self) -> None:
        result = CommandResult(
            status=CommandStatus.SUCCESS,
            message="Done",
            command_name="status",
        )
        assert result.status == CommandStatus.SUCCESS

    def test_to_dict(self) -> None:
        result = CommandResult(
            status=CommandStatus.ERROR,
            message="Failed",
            command_name="test",
            data={"key": "value"},
        )
        d = result.to_dict()
        assert d["status"] == "error"
        assert d["data"]["key"] == "value"


# ── ChatCommandRegistry ─────────────────────────────────────────────────────


class TestChatCommandRegistry:
    def test_builtin_commands_registered(self) -> None:
        registry = ChatCommandRegistry()
        cmds = registry.list_commands()
        names = [c["name"] for c in cmds]
        assert "status" in names
        assert "new" in names
        assert "reset" in names
        assert "compact" in names
        assert "think" in names
        assert "verbose" in names
        assert "usage" in names
        assert "help" in names

    def test_execute_status(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/status", context={"runtime": {"state": "running"}})
        assert result.status == CommandStatus.SUCCESS
        assert result.data["state"] == "running"

    def test_execute_new(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/new my_session")
        assert result.status == CommandStatus.SUCCESS
        assert "my_session" in result.message

    def test_execute_reset(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/reset")
        assert result.status == CommandStatus.SUCCESS

    def test_execute_reset_full(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/reset --full")
        assert result.status == CommandStatus.SUCCESS
        assert "full" in result.message.lower()

    def test_execute_compact(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/compact --strategy prune")
        assert result.status == CommandStatus.SUCCESS
        assert result.data["strategy"] == "prune"

    def test_execute_think(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/think deep")
        assert result.status == CommandStatus.SUCCESS
        assert result.data["mode"] == "deep"

    def test_execute_think_invalid(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/think invalid_mode")
        assert result.status == CommandStatus.ERROR

    def test_execute_verbose_on(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/verbose on")
        assert result.data["verbose"] is True

    def test_execute_verbose_off(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/verbose off")
        assert result.data["verbose"] is False

    def test_execute_verbose_toggle(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/verbose", context={"verbose": True})
        assert result.data["verbose"] is False

    def test_execute_usage(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute(
            "/usage",
            context={
                "usage": {"total_tokens": 5000, "total_cost": 1.23},
            },
        )
        assert result.status == CommandStatus.SUCCESS
        assert result.data["total_tokens"] == 5000

    def test_execute_help(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/help")
        assert result.status == CommandStatus.SUCCESS
        assert result.data["command_count"] > 0

    def test_unknown_command(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/nonexistent")
        assert result.status == CommandStatus.NOT_FOUND

    def test_alias_resolution(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/s")  # alias for /status
        assert result.status == CommandStatus.SUCCESS

    def test_help_alias(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("/?")
        assert result.status == CommandStatus.SUCCESS

    def test_register_custom_command(self) -> None:
        registry = ChatCommandRegistry()

        def my_handler(cmd: ParsedCommand, ctx: dict) -> CommandResult:  # type: ignore[type-arg]
            return CommandResult(
                status=CommandStatus.SUCCESS,
                message="Custom!",
                data={"custom": True},
            )

        registry.register(
            CommandDefinition(
                name="deploy",
                description="Deploy the build",
                handler=my_handler,
                permission=CommandPermission.ADMIN,
            )
        )
        result = registry.execute(
            "/deploy",
            user_role=CommandPermission.ADMIN,
        )
        assert result.status == CommandStatus.SUCCESS
        assert result.data["custom"] is True

    def test_permission_denied(self) -> None:
        registry = ChatCommandRegistry()

        def admin_handler(cmd: ParsedCommand, ctx: dict) -> CommandResult:  # type: ignore[type-arg]
            return CommandResult(status=CommandStatus.SUCCESS, message="OK")

        registry.register(
            CommandDefinition(
                name="admin_cmd",
                description="Admin only",
                handler=admin_handler,
                permission=CommandPermission.ADMIN,
            )
        )
        result = registry.execute(
            "/admin_cmd",
            user_role=CommandPermission.PUBLIC,
        )
        assert result.status == CommandStatus.DENIED

    def test_operator_permission(self) -> None:
        registry = ChatCommandRegistry()

        def op_handler(cmd: ParsedCommand, ctx: dict) -> CommandResult:  # type: ignore[type-arg]
            return CommandResult(status=CommandStatus.SUCCESS, message="OK")

        registry.register(
            CommandDefinition(
                name="op_cmd",
                description="Operator command",
                handler=op_handler,
                permission=CommandPermission.OPERATOR,
            )
        )
        result = registry.execute(
            "/op_cmd",
            user_role=CommandPermission.OPERATOR,
        )
        assert result.status == CommandStatus.SUCCESS

    def test_disabled_command(self) -> None:
        registry = ChatCommandRegistry()

        def handler(cmd: ParsedCommand, ctx: dict) -> CommandResult:  # type: ignore[type-arg]
            return CommandResult(status=CommandStatus.SUCCESS, message="OK")

        registry.register(
            CommandDefinition(
                name="disabled_cmd",
                description="Disabled",
                handler=handler,
                enabled=False,
            )
        )
        result = registry.execute("/disabled_cmd")
        assert result.status == CommandStatus.DISABLED

    def test_disabled_channel(self) -> None:
        registry = ChatCommandRegistry()
        registry.disable_channel("restricted")
        result = registry.execute("/status", channel="restricted")
        assert result.status == CommandStatus.DISABLED

    def test_enable_channel(self) -> None:
        registry = ChatCommandRegistry()
        registry.disable_channel("ch1")
        registry.enable_channel("ch1")
        result = registry.execute("/status", channel="ch1")
        assert result.status == CommandStatus.SUCCESS

    def test_cooldown(self) -> None:
        registry = ChatCommandRegistry()

        def handler(cmd: ParsedCommand, ctx: dict) -> CommandResult:  # type: ignore[type-arg]
            return CommandResult(status=CommandStatus.SUCCESS, message="OK")

        registry.register(
            CommandDefinition(
                name="rate_limited_cmd",
                description="Has cooldown",
                handler=handler,
                cooldown_seconds=60.0,
            )
        )
        result1 = registry.execute("/rate_limited_cmd", channel="ch1")
        assert result1.status == CommandStatus.SUCCESS
        result2 = registry.execute("/rate_limited_cmd", channel="ch1")
        assert result2.status == CommandStatus.RATE_LIMITED

    def test_unregister(self) -> None:
        registry = ChatCommandRegistry()
        assert registry.unregister("status")
        result = registry.execute("/status")
        assert result.status == CommandStatus.NOT_FOUND

    def test_unregister_nonexistent(self) -> None:
        registry = ChatCommandRegistry()
        assert not registry.unregister("nonexistent")

    def test_stats(self) -> None:
        registry = ChatCommandRegistry()
        registry.execute("/status")
        registry.execute("/help")
        stats = registry.stats()
        assert stats["total_executions"] == 2
        assert stats["total_commands"] >= 8

    def test_handler_exception(self) -> None:
        registry = ChatCommandRegistry()

        def bad_handler(cmd: ParsedCommand, ctx: dict) -> CommandResult:  # type: ignore[type-arg]
            raise ValueError("boom")

        registry.register(
            CommandDefinition(
                name="broken",
                description="Broken command",
                handler=bad_handler,
            )
        )
        result = registry.execute("/broken")
        assert result.status == CommandStatus.ERROR
        assert "boom" in result.message

    def test_invalid_parse(self) -> None:
        registry = ChatCommandRegistry()
        result = registry.execute("not a command")
        assert result.status == CommandStatus.ERROR

    def test_list_commands_hidden(self) -> None:
        registry = ChatCommandRegistry()

        def handler(cmd: ParsedCommand, ctx: dict) -> CommandResult:  # type: ignore[type-arg]
            return CommandResult(status=CommandStatus.SUCCESS, message="OK")

        registry.register(
            CommandDefinition(
                name="secret_cmd",
                description="Secret",
                handler=handler,
                hidden=True,
            )
        )
        visible = registry.list_commands(include_hidden=False)
        hidden = registry.list_commands(include_hidden=True)
        assert len(hidden) > len(visible)
