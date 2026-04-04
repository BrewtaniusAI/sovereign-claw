"""Tests for sovereign_claw.plugin_sdk."""

from __future__ import annotations

import pytest

from sovereign_claw.plugin_sdk import (
    PluginHook,
    PluginManifest,
    PluginPermission,
    PluginSandbox,
    PluginSDK,
    PluginState,
    PluginTrust,
)


# ── PluginManifest ───────────────────────────────────────────────────────────


class TestPluginManifest:
    def test_creation(self) -> None:
        m = PluginManifest(name="test_plugin", version="1.0.0")
        assert m.name == "test_plugin"
        assert m.version == "1.0.0"

    def test_plugin_id(self) -> None:
        m = PluginManifest(name="my_plugin", version="2.0.0")
        assert m.plugin_id == "my_plugin@2.0.0"

    def test_compute_hash(self) -> None:
        m = PluginManifest(name="test", version="1.0.0")
        h = m.compute_hash()
        assert len(h) == 16
        # Deterministic
        assert m.compute_hash() == h

    def test_different_manifests_different_hash(self) -> None:
        m1 = PluginManifest(name="plugin_a", version="1.0.0")
        m2 = PluginManifest(name="plugin_b", version="1.0.0")
        assert m1.compute_hash() != m2.compute_hash()

    def test_to_dict(self) -> None:
        m = PluginManifest(
            name="test",
            version="1.0.0",
            author="dev",
            permissions=[PluginPermission.READ_CONTEXT],
            hooks=[PluginHook.POST_EXECUTION],
        )
        d = m.to_dict()
        assert d["name"] == "test"
        assert "read_context" in d["permissions"]
        assert "post_execution" in d["hooks"]
        assert "hash" in d


# ── PluginTrust ──────────────────────────────────────────────────────────────


class TestPluginTrust:
    def test_initial_trust(self) -> None:
        t = PluginTrust()
        assert t.trust_score == 1.0
        assert not t.should_block

    def test_record_invocation(self) -> None:
        t = PluginTrust()
        t.record_invocation()
        assert t.total_invocations == 1

    def test_record_error_decreases_trust(self) -> None:
        t = PluginTrust()
        t.record_error()
        assert t.trust_score < 1.0
        assert t.total_errors == 1

    def test_record_violation_decreases_trust(self) -> None:
        t = PluginTrust()
        t.record_violation("test violation")
        assert t.trust_score < 1.0
        assert t.total_violations == 1
        assert t.last_violation == "test violation"

    def test_trust_floor(self) -> None:
        t = PluginTrust()
        for _ in range(100):
            t.record_violation("spam")
        assert t.trust_score >= 0.0

    def test_should_block_at_low_trust(self) -> None:
        t = PluginTrust()
        for _ in range(10):
            t.record_violation("bad")
        assert t.should_block

    def test_to_dict(self) -> None:
        t = PluginTrust()
        t.record_invocation()
        d = t.to_dict()
        assert d["total_invocations"] == 1


# ── PluginSandbox ────────────────────────────────────────────────────────────


class TestPluginSandbox:
    def test_check_permission_granted(self) -> None:
        sandbox = PluginSandbox([PluginPermission.READ_CONTEXT])
        assert sandbox.check_permission(PluginPermission.READ_CONTEXT)

    def test_check_permission_denied(self) -> None:
        sandbox = PluginSandbox([PluginPermission.READ_CONTEXT])
        assert not sandbox.check_permission(PluginPermission.FILE_WRITE)

    def test_admin_grants_all(self) -> None:
        sandbox = PluginSandbox([PluginPermission.ADMIN])
        assert sandbox.check_permission(PluginPermission.NETWORK_ACCESS)
        assert sandbox.check_permission(PluginPermission.FILE_WRITE)

    def test_require_permission_raises(self) -> None:
        sandbox = PluginSandbox([])
        with pytest.raises(PermissionError):
            sandbox.require_permission(PluginPermission.EXECUTE_TOOLS)

    def test_granted_permissions_list(self) -> None:
        perms = [PluginPermission.READ_CONTEXT, PluginPermission.NETWORK_ACCESS]
        sandbox = PluginSandbox(perms)
        assert set(sandbox.granted_permissions) == set(perms)


# ── PluginSDK ────────────────────────────────────────────────────────────────


class TestPluginSDK:
    def _make_manifest(
        self,
        name: str = "test_plugin",
        version: str = "1.0.0",
        permissions: list[PluginPermission] | None = None,
        hooks: list[PluginHook] | None = None,
        dependencies: list[str] | None = None,
    ) -> PluginManifest:
        return PluginManifest(
            name=name,
            version=version,
            permissions=permissions or [PluginPermission.READ_CONTEXT],
            hooks=hooks or [PluginHook.POST_EXECUTION],
            dependencies=dependencies or [],
        )

    def test_register(self) -> None:
        sdk = PluginSDK()
        m = self._make_manifest()
        instance = sdk.register(m)
        assert instance.state == PluginState.DISCOVERED

    def test_register_max_plugins(self) -> None:
        sdk = PluginSDK()
        sdk.MAX_PLUGINS = 2
        sdk.register(self._make_manifest(name="p1"))
        sdk.register(self._make_manifest(name="p2"))
        with pytest.raises(RuntimeError, match="limit"):
            sdk.register(self._make_manifest(name="p3"))

    def test_register_disallowed_permission(self) -> None:
        sdk = PluginSDK(allowed_permissions=[PluginPermission.READ_CONTEXT])
        m = self._make_manifest(permissions=[PluginPermission.ADMIN])
        with pytest.raises(PermissionError):
            sdk.register(m)

    def test_load(self) -> None:
        sdk = PluginSDK()
        sdk.register(self._make_manifest())
        instance = sdk.load("test_plugin")
        assert instance.state == PluginState.LOADED

    def test_load_unknown_plugin(self) -> None:
        sdk = PluginSDK()
        with pytest.raises(KeyError):
            sdk.load("nonexistent")

    def test_load_missing_dependency(self) -> None:
        sdk = PluginSDK()
        sdk.register(self._make_manifest(name="dep_plugin", dependencies=["base_plugin"]))
        with pytest.raises(RuntimeError, match="Missing dependency"):
            sdk.load("dep_plugin")

    def test_load_with_dependency(self) -> None:
        sdk = PluginSDK()
        sdk.register(self._make_manifest(name="base"))
        sdk.load("base")
        sdk.register(self._make_manifest(name="child", dependencies=["base"]))
        instance = sdk.load("child")
        assert instance.state == PluginState.LOADED

    def test_enable(self) -> None:
        sdk = PluginSDK()
        sdk.register(self._make_manifest())
        sdk.load("test_plugin")
        instance = sdk.enable("test_plugin")
        assert instance.state == PluginState.ENABLED

    def test_enable_blocked_trust(self) -> None:
        sdk = PluginSDK()
        sdk.register(self._make_manifest())
        sdk.load("test_plugin")
        # Tank trust
        inst = sdk.get_plugin("test_plugin")
        assert inst is not None
        for _ in range(20):
            inst.trust.record_violation("bad")
        with pytest.raises(RuntimeError, match="blocked"):
            sdk.enable("test_plugin")

    def test_disable(self) -> None:
        sdk = PluginSDK()
        sdk.register(self._make_manifest())
        sdk.load("test_plugin")
        sdk.enable("test_plugin")
        instance = sdk.disable("test_plugin")
        assert instance.state == PluginState.DISABLED

    def test_unload(self) -> None:
        sdk = PluginSDK()
        sdk.register(self._make_manifest())
        sdk.load("test_plugin")
        assert sdk.unload("test_plugin")

    def test_unload_nonexistent(self) -> None:
        sdk = PluginSDK()
        assert not sdk.unload("nope")

    def test_remove(self) -> None:
        sdk = PluginSDK()
        sdk.register(self._make_manifest())
        assert sdk.remove("test_plugin")
        assert sdk.get_plugin("test_plugin") is None

    def test_remove_nonexistent(self) -> None:
        sdk = PluginSDK()
        assert not sdk.remove("nope")

    def test_execute_hook(self) -> None:
        sdk = PluginSDK()
        m = self._make_manifest(hooks=[PluginHook.POST_EXECUTION])
        sdk.register(m)
        instance = sdk.load("test_plugin")
        # Manually register a handler
        instance.hook_handlers[PluginHook.POST_EXECUTION] = lambda sandbox, **kwargs: "hook_result"
        sdk.enable("test_plugin")
        results = sdk.execute_hook(PluginHook.POST_EXECUTION)
        assert len(results) == 1
        assert results[0] == ("test_plugin", "hook_result")

    def test_execute_hook_permission_violation(self) -> None:
        sdk = PluginSDK()
        m = self._make_manifest(
            permissions=[PluginPermission.READ_CONTEXT],
            hooks=[PluginHook.PRE_TOOL_CALL],
        )
        sdk.register(m)
        instance = sdk.load("test_plugin")

        def bad_handler(sandbox: PluginSandbox, **kwargs: object) -> str:
            sandbox.require_permission(PluginPermission.ADMIN)
            return "should not reach"

        instance.hook_handlers[PluginHook.PRE_TOOL_CALL] = bad_handler
        sdk.enable("test_plugin")
        results = sdk.execute_hook(PluginHook.PRE_TOOL_CALL)
        # No results because permission denied
        assert len(results) == 0
        # Trust decreased
        trust = sdk.get_trust("test_plugin")
        assert trust is not None
        assert trust.total_violations > 0

    def test_execute_hook_error(self) -> None:
        sdk = PluginSDK()
        m = self._make_manifest(hooks=[PluginHook.ON_ERROR])
        sdk.register(m)
        instance = sdk.load("test_plugin")
        instance.hook_handlers[PluginHook.ON_ERROR] = lambda sandbox, **kwargs: 1 / 0
        sdk.enable("test_plugin")
        results = sdk.execute_hook(PluginHook.ON_ERROR)
        assert len(results) == 1
        assert isinstance(results[0][1], ZeroDivisionError)

    def test_list_plugins(self) -> None:
        sdk = PluginSDK()
        sdk.register(self._make_manifest(name="p1"))
        sdk.register(self._make_manifest(name="p2"))
        all_plugins = sdk.list_plugins()
        assert len(all_plugins) == 2

    def test_list_plugins_by_state(self) -> None:
        sdk = PluginSDK()
        sdk.register(self._make_manifest(name="p1"))
        sdk.register(self._make_manifest(name="p2"))
        sdk.load("p1")
        loaded = sdk.list_plugins(state=PluginState.LOADED)
        assert len(loaded) == 1

    def test_stats(self) -> None:
        sdk = PluginSDK()
        sdk.register(self._make_manifest())
        stats = sdk.stats()
        assert stats["total_plugins"] == 1
        assert "by_state" in stats

    def test_auto_block_after_violations(self) -> None:
        sdk = PluginSDK()
        m = self._make_manifest(
            permissions=[],
            hooks=[PluginHook.ON_MESSAGE],
        )
        sdk.register(m)
        instance = sdk.load("test_plugin")

        def violating_handler(sandbox: PluginSandbox, **kwargs: object) -> str:
            sandbox.require_permission(PluginPermission.ADMIN)
            return "nope"

        instance.hook_handlers[PluginHook.ON_MESSAGE] = violating_handler
        sdk.enable("test_plugin")

        # Execute enough times to trigger auto-block
        for _ in range(10):
            sdk.execute_hook(PluginHook.ON_MESSAGE)

        inst = sdk.get_plugin("test_plugin")
        assert inst is not None
        assert inst.state == PluginState.BLOCKED
