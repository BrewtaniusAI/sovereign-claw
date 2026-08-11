"""
plugin_sdk — Extensible Plugin Architecture
=============================================
Governed plugin system with lifecycle management and sandboxing.

Features:
- Plugin manifest system (name, version, author, permissions, entry point)
- Plugin lifecycle management (discover, load, enable, disable, unload)
- Dependency resolution between plugins
- Permission-scoped execution sandboxing
- Plugin trust scoring and violation tracking
- Hot-reload support for development
- Governed plugins: all plugin actions auditable via ProofVault

Plugins extend the sovereign runtime without modifying core code.
Every plugin operates under governance constraints.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class PluginState(str, Enum):
    """Lifecycle state of a plugin."""

    DISCOVERED = "discovered"
    LOADED = "loaded"
    ENABLED = "enabled"
    DISABLED = "disabled"
    UNLOADED = "unloaded"
    FAILED = "failed"
    BLOCKED = "blocked"


class PluginPermission(str, Enum):
    """Permissions a plugin can request."""

    READ_CONTEXT = "read_context"
    WRITE_CONTEXT = "write_context"
    EXECUTE_TOOLS = "execute_tools"
    NETWORK_ACCESS = "network_access"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    MODEL_ACCESS = "model_access"
    MEMORY_ACCESS = "memory_access"
    EVENT_PUBLISH = "event_publish"
    EVENT_SUBSCRIBE = "event_subscribe"
    ADMIN = "admin"


class PluginHook(str, Enum):
    """Hook points where plugins can attach."""

    PRE_EXECUTION = "pre_execution"
    POST_EXECUTION = "post_execution"
    PRE_TOOL_CALL = "pre_tool_call"
    POST_TOOL_CALL = "post_tool_call"
    ON_MESSAGE = "on_message"
    ON_ERROR = "on_error"
    ON_DRIFT_CHANGE = "on_drift_change"
    ON_POLICY_CHECK = "on_policy_check"
    ON_COMPACTION = "on_compaction"
    ON_STARTUP = "on_startup"
    ON_SHUTDOWN = "on_shutdown"


@dataclass
class PluginManifest:
    """Plugin manifest defining metadata and requirements."""

    name: str
    version: str
    author: str = ""
    description: str = ""
    entry_point: str = ""
    permissions: list[PluginPermission] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)  # other plugin names
    hooks: list[PluginHook] = field(default_factory=list)
    min_runtime_version: str = "3.0.0"
    tags: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    trusted_in_process: bool = False

    @property
    def plugin_id(self) -> str:
        return f"{self.name}@{self.version}"

    def compute_hash(self) -> str:
        """Compute a deterministic hash of the manifest."""
        parts = [
            self.name,
            self.version,
            self.author,
            self.description,
            self.entry_point,
            str(sorted(p.value for p in self.permissions)),
            str(sorted(self.dependencies)),
            str(sorted(h.value for h in self.hooks)),
        ]
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "author": self.author,
            "description": self.description,
            "entry_point": self.entry_point,
            "permissions": [p.value for p in self.permissions],
            "dependencies": self.dependencies,
            "hooks": [h.value for h in self.hooks],
            "min_runtime_version": self.min_runtime_version,
            "tags": self.tags,
            "hash": self.compute_hash(),
            "trusted_in_process": self.trusted_in_process,
        }


@dataclass
class PluginTrust:
    """Trust metrics for a plugin."""

    trust_score: float = 1.0
    total_invocations: int = 0
    total_errors: int = 0
    total_violations: int = 0
    last_violation: str = ""
    last_violation_time: float = 0.0
    approved_by: str = ""
    approved_at: float = 0.0

    # Trust decay constants
    VIOLATION_PENALTY = 0.15
    ERROR_PENALTY = 0.05
    MIN_TRUST = 0.0
    MAX_TRUST = 1.0
    BLOCK_THRESHOLD = 0.3

    def record_invocation(self) -> None:
        self.total_invocations += 1

    def record_error(self) -> None:
        self.total_errors += 1
        self.trust_score = max(
            self.MIN_TRUST,
            self.trust_score - self.ERROR_PENALTY,
        )

    def record_violation(self, reason: str) -> None:
        self.total_violations += 1
        self.last_violation = reason
        self.last_violation_time = time.time()
        self.trust_score = max(
            self.MIN_TRUST,
            self.trust_score - self.VIOLATION_PENALTY,
        )

    @property
    def should_block(self) -> bool:
        return self.trust_score < self.BLOCK_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        return {
            "trust_score": round(self.trust_score, 3),
            "total_invocations": self.total_invocations,
            "total_errors": self.total_errors,
            "total_violations": self.total_violations,
            "last_violation": self.last_violation,
            "should_block": self.should_block,
        }


# Type for plugin hook handlers
HookHandler = Callable[..., Any]


@dataclass
class PluginInstance:
    """A loaded plugin instance."""

    manifest: PluginManifest
    state: PluginState = PluginState.DISCOVERED
    trust: PluginTrust = field(default_factory=PluginTrust)
    config: dict[str, Any] = field(default_factory=dict)
    instance_id: str = ""
    loaded_at: float = 0.0
    module: Any = None
    hook_handlers: dict[PluginHook, HookHandler] = field(default_factory=dict)
    error: str = ""

    def __post_init__(self) -> None:
        if not self.instance_id:
            self.instance_id = f"pi_{uuid.uuid4().hex[:10]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "manifest": self.manifest.to_dict(),
            "state": self.state.value,
            "trust": self.trust.to_dict(),
            "config": self.config,
            "loaded_at": self.loaded_at,
            "error": self.error,
        }


class PluginSandbox:
    """
    Permission-scoped execution sandbox for plugins.

    Validates that a plugin has the required permissions before
    allowing an operation.

    **Note:** This sandbox is advisory/cooperative only. It checks that the
    plugin declares and is granted the required permissions, but does NOT
    provide OS-level process isolation, filesystem isolation, or network
    isolation. A malicious or buggy plugin can bypass these checks by calling
    system APIs directly. For untrusted plugins, use additional isolation
    such as subprocess sandboxing, containers, or a separate OS process with
    restricted capabilities.
    """

    def __init__(self, granted: list[PluginPermission]) -> None:
        self._granted = set(granted)

    def check_permission(self, required: PluginPermission) -> bool:
        """Check if a permission is granted."""
        if PluginPermission.ADMIN in self._granted:
            return True
        return required in self._granted

    def require_permission(self, required: PluginPermission) -> None:
        """Require a permission or raise."""
        if not self.check_permission(required):
            raise PermissionError(f"Plugin lacks required permission: {required.value}")

    @property
    def granted_permissions(self) -> list[PluginPermission]:
        return list(self._granted)


class PluginSDK:
    """
    Plugin management system with lifecycle, sandboxing, and trust.

    Usage:
        sdk = PluginSDK()

        # Register a plugin
        manifest = PluginManifest(
            name="my_plugin",
            version="1.0.0",
            permissions=[PluginPermission.READ_CONTEXT],
            hooks=[PluginHook.POST_EXECUTION],
        )
        sdk.register(manifest)

        # Load and enable
        sdk.load("my_plugin")
        sdk.enable("my_plugin")

        # Execute a hook
        results = sdk.execute_hook(PluginHook.POST_EXECUTION, context={})

        # Check trust
        trust = sdk.get_trust("my_plugin")
    """

    # Maximum plugins
    MAX_PLUGINS = 100

    # Maximum violations before auto-block
    MAX_VIOLATIONS_BEFORE_BLOCK = 5

    def __init__(
        self,
        allowed_permissions: list[PluginPermission] | None = None,
        trusted_in_process_allowlist: set[str] | None = None,
    ) -> None:
        self._plugins: dict[str, PluginInstance] = {}
        self._allowed_permissions = set(allowed_permissions or list(PluginPermission))
        self._trusted_in_process_allowlist = set(trusted_in_process_allowlist or set())
        self._hook_registry: dict[PluginHook, list[str]] = {h: [] for h in PluginHook}
        self._total_hooks_executed = 0
        self._total_errors = 0

    @staticmethod
    def _manifest_security_identity(manifest: PluginManifest) -> str:
        payload = {
            "name": manifest.name,
            "version": manifest.version,
            "author": manifest.author,
            "description": manifest.description,
            "entry_point": manifest.entry_point,
            "permissions": sorted(p.value for p in manifest.permissions),
            "dependencies": sorted(manifest.dependencies),
            "hooks": sorted(h.value for h in manifest.hooks),
            "min_runtime_version": manifest.min_runtime_version,
            "tags": sorted(manifest.tags),
        }
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    def register(
        self,
        manifest: PluginManifest,
        config: dict[str, Any] | None = None,
    ) -> PluginInstance:
        """Register a plugin from its manifest."""
        if len(self._plugins) >= self.MAX_PLUGINS:
            raise RuntimeError(f"Plugin limit reached ({self.MAX_PLUGINS})")

        if manifest.name in self._plugins:
            raise ValueError(
                f"Plugin {manifest.name!r} is already registered. "
                "Call remove() or unload() first if replacement is intended."
            )

        # Check permissions are allowed
        for perm in manifest.permissions:
            if perm not in self._allowed_permissions:
                raise PermissionError(f"Permission {perm.value} not allowed by runtime")

        instance = PluginInstance(
            manifest=manifest,
            state=PluginState.DISCOVERED,
            config=config or {},
        )
        self._plugins[manifest.name] = instance
        return instance

    def load(self, name: str) -> PluginInstance:
        """Load a plugin (resolve dependencies, import module)."""
        instance = self._get_plugin(name)

        # Check dependencies
        for dep in instance.manifest.dependencies:
            dep_instance = self._plugins.get(dep)
            if not dep_instance:
                instance.state = PluginState.FAILED
                instance.error = f"Missing dependency: {dep}"
                raise RuntimeError(instance.error)
            if dep_instance.state not in (PluginState.LOADED, PluginState.ENABLED):
                instance.state = PluginState.FAILED
                instance.error = f"Dependency not loaded: {dep}"
                raise RuntimeError(instance.error)

        # Load module if entry_point specified
        if instance.manifest.entry_point:
            instance.state = PluginState.BLOCKED
            instance.error = (
                "Untrusted plugin import blocked: dynamic in-process entry_point import requires "
                "server-owned package/provenance trust verification"
            )
            raise RuntimeError(instance.error)

        instance.state = PluginState.LOADED
        instance.loaded_at = time.time()
        return instance

    def enable(self, name: str) -> PluginInstance:
        """Enable a loaded plugin."""
        instance = self._get_plugin(name)

        if instance.state not in (PluginState.LOADED, PluginState.DISABLED):
            raise RuntimeError(f"Cannot enable plugin in state: {instance.state.value}")

        if instance.trust.should_block:
            instance.state = PluginState.BLOCKED
            raise RuntimeError(
                f"Plugin blocked due to low trust score: {instance.trust.trust_score:.2f}"
            )

        instance.state = PluginState.ENABLED

        # Register hooks
        for hook in instance.manifest.hooks:
            if name not in self._hook_registry[hook]:
                self._hook_registry[hook].append(name)

        return instance

    def disable(self, name: str) -> PluginInstance:
        """Disable an enabled plugin."""
        instance = self._get_plugin(name)
        instance.state = PluginState.DISABLED

        # Unregister hooks
        for hook in PluginHook.__members__.values():
            if name in self._hook_registry[hook]:
                self._hook_registry[hook].remove(name)

        return instance

    def unload(self, name: str) -> bool:
        """Unload a plugin completely."""
        instance = self._plugins.get(name)
        if not instance:
            return False
        self.disable(name) if instance.state == PluginState.ENABLED else None
        instance.state = PluginState.UNLOADED
        instance.module = None
        instance.hook_handlers.clear()
        return True

    def remove(self, name: str) -> bool:
        """Remove a plugin entirely."""
        if name in self._plugins:
            self.unload(name)
            del self._plugins[name]
            return True
        return False

    def execute_hook(
        self,
        hook: PluginHook,
        **kwargs: Any,
    ) -> list[tuple[str, Any]]:
        """
        Execute all plugin handlers registered for a hook.

        Returns list of (plugin_name, result) tuples.
        """
        results: list[tuple[str, Any]] = []
        for name in list(self._hook_registry.get(hook, [])):
            instance = self._plugins.get(name)
            if not instance or instance.state != PluginState.ENABLED:
                continue

            handler = instance.hook_handlers.get(hook)
            if not handler:
                continue

            instance.trust.record_invocation()
            self._total_hooks_executed += 1

            sandbox = PluginSandbox(instance.manifest.permissions)
            try:
                result = handler(sandbox=sandbox, **kwargs)
                results.append((name, result))
            except PermissionError as exc:
                instance.trust.record_violation(f"Permission denied: {exc}")
                self._total_errors += 1
                if instance.trust.total_violations >= self.MAX_VIOLATIONS_BEFORE_BLOCK:
                    instance.state = PluginState.BLOCKED
                    self._unregister_hooks(name)
            except Exception as exc:
                instance.trust.record_error()
                self._total_errors += 1
                results.append((name, exc))

        return results

    def get_plugin(self, name: str) -> PluginInstance | None:
        """Get a plugin instance by name."""
        return self._plugins.get(name)

    def get_trust(self, name: str) -> PluginTrust | None:
        """Get trust metrics for a plugin."""
        instance = self._plugins.get(name)
        return instance.trust if instance else None

    def list_plugins(
        self,
        state: PluginState | None = None,
    ) -> list[PluginInstance]:
        """List all plugins, optionally filtered by state."""
        plugins = list(self._plugins.values())
        if state:
            plugins = [p for p in plugins if p.state == state]
        return plugins

    def stats(self) -> dict[str, Any]:
        """Get SDK statistics."""
        by_state: dict[str, int] = {}
        for p in self._plugins.values():
            by_state[p.state.value] = by_state.get(p.state.value, 0) + 1
        return {
            "total_plugins": len(self._plugins),
            "total_hooks_executed": self._total_hooks_executed,
            "total_errors": self._total_errors,
            "by_state": by_state,
            "hook_registrations": {
                h.value: len(names) for h, names in self._hook_registry.items() if names
            },
        }

    def _get_plugin(self, name: str) -> PluginInstance:
        instance = self._plugins.get(name)
        if not instance:
            raise KeyError(f"Unknown plugin: {name}")
        return instance

    def _unregister_hooks(self, name: str) -> None:
        for hook in PluginHook.__members__.values():
            if name in self._hook_registry[hook]:
                self._hook_registry[hook].remove(name)
