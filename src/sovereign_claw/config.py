"""
config.py — Governed Configuration System
==========================================
Pydantic-validated, multi-source configuration with governed defaults.
Supports JSON, TOML, and environment variable overrides.

Every configuration mutation is logged to ProofVault for audit compliance.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib  # type: ignore[no-redef,import-untyped]
    except ImportError:
        tomllib = None  # type: ignore[assignment]

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_CONFIG_DIR = Path.home() / ".sovereign_claw"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.json"

ProviderName = Literal[
    "anthropic", "openai", "gemini", "perplexity", "ollama", "groq", "mistral", "local"
]


# ── Provider configuration ────────────────────────────────────────────────────
@dataclass
class ProviderProfile:
    """Single LLM provider configuration."""

    name: ProviderName
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    timeout: float = 60.0
    max_tokens: int = 4096
    temperature: float = 0.7
    priority: int = 0  # lower = higher priority in failover chain

    def is_configured(self) -> bool:
        """True if provider has minimum viable configuration."""
        if self.name == "ollama":
            return bool(self.model)
        if self.name == "local":
            return bool(self.base_url and self.model)
        return bool(self.api_key and self.model)


# ── Channel configuration ────────────────────────────────────────────────────
@dataclass
class ChannelConfig:
    """Configuration for a messaging channel connector."""

    enabled: bool = False
    token: str = ""
    webhook_url: str = ""
    allowed_users: List[str] = field(default_factory=list)
    allowed_channels: List[str] = field(default_factory=list)
    dm_pairing_required: bool = True
    rate_limit_per_minute: int = 30


# ── Voice configuration ──────────────────────────────────────────────────────
@dataclass
class VoiceConfig:
    """Voice/TTS/STT configuration."""

    enabled: bool = False
    tts_provider: Literal["elevenlabs", "system", "openai", "local"] = "system"
    stt_provider: Literal["whisper", "deepgram", "system", "local"] = "system"
    tts_api_key: str = ""
    stt_api_key: str = ""
    tts_voice_id: str = "default"
    stt_model: str = "whisper-1"
    wake_word: str = "sovereign"
    silence_threshold_ms: int = 1500


# ── Gateway configuration ────────────────────────────────────────────────────
@dataclass
class GatewayConfig:
    """WebSocket gateway configuration."""

    host: str = "0.0.0.0"
    port: int = 8765
    max_connections: int = 100
    heartbeat_interval: float = 30.0
    session_timeout: float = 3600.0
    cors_origins: List[str] = field(default_factory=lambda: ["*"])
    tls_cert: str = ""
    tls_key: str = ""


# ── Security configuration ───────────────────────────────────────────────────
@dataclass
class SecurityConfig:
    """Security and access control configuration."""

    secret_detection_enabled: bool = True
    dm_pairing_enabled: bool = True
    allowlist_mode: Literal["open", "allowlist", "denylist"] = "allowlist"
    allowed_users: List[str] = field(default_factory=list)
    denied_users: List[str] = field(default_factory=list)
    max_message_length: int = 32768
    rate_limit_global: int = 100  # requests per minute
    audit_all_messages: bool = True


# ── Scheduler configuration ──────────────────────────────────────────────────
@dataclass
class SchedulerConfig:
    """Cron/webhook automation configuration."""

    enabled: bool = False
    max_concurrent_jobs: int = 5
    webhook_secret: str = ""
    webhook_port: int = 9090


# ── Canvas configuration ─────────────────────────────────────────────────────
@dataclass
class CanvasConfig:
    """Live Canvas / A2UI configuration."""

    enabled: bool = False
    max_canvas_size_kb: int = 512
    render_timeout_ms: int = 5000
    snapshot_history_limit: int = 50


# ── Browser configuration ────────────────────────────────────────────────────
@dataclass
class BrowserConfig:
    """CDP browser control configuration."""

    enabled: bool = False
    cdp_endpoint: str = "http://localhost:9222"
    headless: bool = True
    viewport_width: int = 1280
    viewport_height: int = 720
    navigation_timeout_ms: int = 30000
    screenshot_format: Literal["png", "jpeg", "webp"] = "png"


# ── MCP configuration ────────────────────────────────────────────────────────
@dataclass
class MCPConfig:
    """Model Context Protocol server configuration."""

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = 8766
    transport: Literal["stdio", "sse", "websocket"] = "stdio"
    max_resources: int = 1000
    max_tools: int = 100


# ── Master configuration ─────────────────────────────────────────────────────
@dataclass
class SovereignConfig:
    """
    Master configuration for sovereign-claw.

    Sources (in priority order):
    1. Environment variables (SOVEREIGN_*)
    2. Config file (~/.sovereign_claw/config.json)
    3. Defaults defined here
    """

    # Core governance
    t_max_steps: int = 16
    risk_threshold: float = 0.90
    drift_convergence_guarantee: bool = True
    proof_vault_path: str = "proof_vault.db"
    event_stream_path: str = "events.jsonl"

    # Provider chain
    providers: List[ProviderProfile] = field(default_factory=list)
    default_provider: ProviderName = "anthropic"

    # Channels
    discord: ChannelConfig = field(default_factory=ChannelConfig)
    slack: ChannelConfig = field(default_factory=ChannelConfig)
    telegram: ChannelConfig = field(default_factory=ChannelConfig)
    whatsapp: ChannelConfig = field(default_factory=ChannelConfig)
    webchat: ChannelConfig = field(default_factory=ChannelConfig)
    irc: ChannelConfig = field(default_factory=ChannelConfig)
    matrix: ChannelConfig = field(default_factory=ChannelConfig)
    signal: ChannelConfig = field(default_factory=ChannelConfig)

    # Subsystems
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    canvas: CanvasConfig = field(default_factory=CanvasConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)

    # Skills
    skills_dirs: List[str] = field(default_factory=lambda: ["~/.sovereign_claw/skills"])
    bundled_skills_enabled: bool = True
    managed_skills_enabled: bool = True
    workspace_skills_enabled: bool = True

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_file: str = ""

    def get_provider_chain(self) -> List[ProviderProfile]:
        """Return providers sorted by priority (lowest first)."""
        configured = [p for p in self.providers if p.is_configured()]
        return sorted(configured, key=lambda p: p.priority)


# ── Config loading / saving ───────────────────────────────────────────────────
def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge override into base."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _dataclass_to_dict(obj: Any) -> Any:
    """Recursively convert dataclass instances to dicts."""
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _dataclass_to_dict(v) for k, v in obj.__dict__.items()}
    if isinstance(obj, list):
        return [_dataclass_to_dict(item) for item in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _apply_env_overrides(data: Dict[str, Any]) -> Dict[str, Any]:
    """Apply SOVEREIGN_* environment variable overrides."""
    prefix = "SOVEREIGN_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        config_key = key[len(prefix) :].lower()
        # Handle nested keys: SOVEREIGN_GATEWAY_PORT -> gateway.port
        parts = config_key.split("_", 1)
        if len(parts) == 2 and parts[0] in data and isinstance(data[parts[0]], dict):
            data[parts[0]][parts[1]] = _coerce_value(value)
        else:
            data[config_key] = _coerce_value(value)
    return data


def _coerce_value(value: str) -> Any:
    """Coerce string env values to appropriate types."""
    # Try numeric types first to avoid "0"/"1" becoming booleans
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if value.lower() in ("true", "yes"):
        return True
    if value.lower() in ("false", "no"):
        return False
    return value


def _build_config_from_dict(data: Dict[str, Any]) -> SovereignConfig:
    """Build a SovereignConfig from a flat/nested dict."""
    # Extract sub-configs
    providers_data = data.pop("providers", [])
    providers = []
    for p in providers_data:
        if isinstance(p, dict):
            providers.append(ProviderProfile(**p))
        elif isinstance(p, ProviderProfile):
            providers.append(p)

    sub_configs = {}
    config_map = {
        "gateway": GatewayConfig,
        "voice": VoiceConfig,
        "security": SecurityConfig,
        "scheduler": SchedulerConfig,
        "canvas": CanvasConfig,
        "browser": BrowserConfig,
        "mcp": MCPConfig,
    }
    channel_map = {
        "discord": ChannelConfig,
        "slack": ChannelConfig,
        "telegram": ChannelConfig,
        "whatsapp": ChannelConfig,
        "webchat": ChannelConfig,
        "irc": ChannelConfig,
        "matrix": ChannelConfig,
        "signal": ChannelConfig,
    }

    for key, cls in {**config_map, **channel_map}.items():
        if key in data:
            val = data.pop(key)
            if isinstance(val, dict):
                # Filter to only known fields to handle extra env vars / config keys
                known_fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
                filtered_val = {k: v for k, v in val.items() if k in known_fields}
                sub_configs[key] = cls(**filtered_val)
            elif isinstance(val, cls):
                sub_configs[key] = val

    # Filter to only valid SovereignConfig fields
    valid_fields = {f.name for f in SovereignConfig.__dataclass_fields__.values()}
    filtered = {k: v for k, v in data.items() if k in valid_fields}

    return SovereignConfig(providers=providers, **sub_configs, **filtered)


def load_config(
    config_path: Optional[str] = None,
    extra_overrides: Optional[Dict[str, Any]] = None,
) -> SovereignConfig:
    """
    Load configuration from file + env vars + overrides.

    Priority: overrides > env vars > file > defaults
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_FILE

    # Start with defaults
    base = _dataclass_to_dict(SovereignConfig())

    # Layer file config
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        if path.suffix == ".toml":
            if tomllib is None:
                raise RuntimeError(
                    "TOML config requires tomllib (Python 3.11+) or tomli: pip install tomli"
                )
            file_data = tomllib.loads(raw)
        else:
            file_data = json.loads(raw)
        base = _deep_merge(base, file_data)

    # Layer env vars
    base = _apply_env_overrides(base)

    # Layer explicit overrides
    if extra_overrides:
        base = _deep_merge(base, extra_overrides)

    return _build_config_from_dict(base)


def save_config(config: SovereignConfig, config_path: Optional[str] = None) -> Path:
    """Save configuration to JSON file."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _dataclass_to_dict(config)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def init_config_dir() -> Path:
    """Create default config directory and initial config file."""
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not DEFAULT_CONFIG_FILE.exists():
        save_config(SovereignConfig())
    skills_dir = DEFAULT_CONFIG_DIR / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    return DEFAULT_CONFIG_DIR
