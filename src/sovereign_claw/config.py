"""
config.py — Governed Configuration System (Pydantic v2)
========================================================
Pydantic-validated, multi-source configuration with governed defaults.
Supports JSON, TOML, .env files, and environment variable overrides.

Every configuration mutation is logged to ProofVault for audit compliance.

Migrated from dataclasses to Pydantic v2 in v3.1.0 to align claims
with implementation and gain field validation, env-var parsing, and
.env file support.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
class ProviderProfile(BaseModel):
    """Single LLM provider configuration with Pydantic validation."""

    model_config = ConfigDict(extra="ignore")

    name: ProviderName
    api_key: str = ""
    model: str = ""
    base_url: str = ""
    timeout: float = Field(default=60.0, gt=0)
    max_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    priority: int = Field(default=0, ge=0)

    def is_configured(self) -> bool:
        """True if provider has minimum viable configuration."""
        if self.name == "ollama":
            return bool(self.model)
        if self.name == "local":
            return bool(self.base_url and self.model)
        return bool(self.api_key and self.model)


# ── Channel configuration ────────────────────────────────────────────────────
class ChannelConfig(BaseModel):
    """Configuration for a messaging channel connector."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    token: str = ""
    webhook_url: str = ""
    allowed_users: List[str] = Field(default_factory=list)
    allowed_channels: List[str] = Field(default_factory=list)
    dm_pairing_required: bool = True
    rate_limit_per_minute: int = Field(default=30, gt=0)


# ── Voice configuration ──────────────────────────────────────────────────────
class VoiceConfig(BaseModel):
    """Voice/TTS/STT configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    tts_provider: Literal["elevenlabs", "system", "openai", "local"] = "system"
    stt_provider: Literal["whisper", "deepgram", "system", "local"] = "system"
    tts_api_key: str = ""
    stt_api_key: str = ""
    tts_voice_id: str = "default"
    stt_model: str = "whisper-1"
    wake_word: str = "sovereign"
    silence_threshold_ms: int = Field(default=1500, gt=0)


# ── Gateway configuration ────────────────────────────────────────────────────
class GatewayConfig(BaseModel):
    """WebSocket gateway configuration."""

    model_config = ConfigDict(extra="ignore")

    host: str = "0.0.0.0"
    port: int = Field(default=8765, ge=1, le=65535)
    max_connections: int = Field(default=100, gt=0)
    heartbeat_interval: float = Field(default=30.0, gt=0)
    session_timeout: float = Field(default=3600.0, gt=0)
    cors_origins: List[str] = Field(default_factory=lambda: ["*"])
    tls_cert: str = ""
    tls_key: str = ""


# ── Security configuration ───────────────────────────────────────────────────
class SecurityConfig(BaseModel):
    """Security and access control configuration."""

    model_config = ConfigDict(extra="ignore")

    secret_detection_enabled: bool = True
    dm_pairing_enabled: bool = True
    allowlist_mode: Literal["open", "allowlist", "denylist"] = "allowlist"
    allowed_users: List[str] = Field(default_factory=list)
    denied_users: List[str] = Field(default_factory=list)
    max_message_length: int = Field(default=32768, gt=0)
    rate_limit_global: int = Field(default=100, gt=0)
    audit_all_messages: bool = True


# ── Scheduler configuration ──────────────────────────────────────────────────
class SchedulerConfig(BaseModel):
    """Cron/webhook automation configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    max_concurrent_jobs: int = Field(default=5, gt=0)
    webhook_secret: str = ""
    webhook_port: int = Field(default=9090, ge=1, le=65535)


# ── Canvas configuration ─────────────────────────────────────────────────────
class CanvasConfig(BaseModel):
    """Live Canvas / A2UI configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    max_canvas_size_kb: int = Field(default=512, gt=0)
    render_timeout_ms: int = Field(default=5000, gt=0)
    snapshot_history_limit: int = Field(default=50, gt=0)


# ── Browser configuration ────────────────────────────────────────────────────
class BrowserConfig(BaseModel):
    """CDP browser control configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    cdp_endpoint: str = "http://localhost:9222"
    headless: bool = True
    viewport_width: int = Field(default=1280, gt=0)
    viewport_height: int = Field(default=720, gt=0)
    navigation_timeout_ms: int = Field(default=30000, gt=0)
    screenshot_format: Literal["png", "jpeg", "webp"] = "png"


# ── MCP configuration ────────────────────────────────────────────────────────
class MCPConfig(BaseModel):
    """Model Context Protocol server configuration."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    host: str = "0.0.0.0"
    port: int = Field(default=8766, ge=1, le=65535)
    transport: Literal["stdio", "sse", "websocket"] = "stdio"
    max_resources: int = Field(default=1000, gt=0)
    max_tools: int = Field(default=100, gt=0)


# ── Master configuration ─────────────────────────────────────────────────────
class SovereignConfig(BaseModel):
    """
    Master configuration for sovereign-claw.

    Pydantic-validated with field constraints and automatic type coercion.

    Sources (in priority order):
    1. Environment variables (SOVEREIGN_*)
    2. Config file (~/.sovereign_claw/config.json)
    3. Defaults defined here
    """

    model_config = ConfigDict(extra="ignore")

    # Core governance
    t_max_steps: int = Field(default=16, gt=0)
    risk_threshold: float = Field(default=0.90, ge=0.0, le=1.0)
    drift_convergence_guarantee: bool = True
    proof_vault_path: str = "proof_vault.db"
    event_stream_path: str = "events.jsonl"

    # Provider chain
    providers: List[ProviderProfile] = Field(default_factory=list)
    default_provider: ProviderName = "anthropic"

    # Channels
    discord: ChannelConfig = Field(default_factory=ChannelConfig)
    slack: ChannelConfig = Field(default_factory=ChannelConfig)
    telegram: ChannelConfig = Field(default_factory=ChannelConfig)
    whatsapp: ChannelConfig = Field(default_factory=ChannelConfig)
    webchat: ChannelConfig = Field(default_factory=ChannelConfig)
    irc: ChannelConfig = Field(default_factory=ChannelConfig)
    matrix: ChannelConfig = Field(default_factory=ChannelConfig)
    signal: ChannelConfig = Field(default_factory=ChannelConfig)

    # Subsystems
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    canvas: CanvasConfig = Field(default_factory=CanvasConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)

    # Skills
    skills_dirs: List[str] = Field(default_factory=lambda: ["~/.sovereign_claw/skills"])
    bundled_skills_enabled: bool = True
    managed_skills_enabled: bool = True
    workspace_skills_enabled: bool = True

    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_file: str = ""

    @field_validator("risk_threshold")
    @classmethod
    def _clamp_risk(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

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


def _model_to_dict(obj: Any) -> Any:
    """Recursively convert Pydantic models (or legacy dataclasses) to dicts."""
    if isinstance(obj, BaseModel):
        return obj.model_dump()
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _model_to_dict(v) for k, v in obj.__dict__.items()}
    if isinstance(obj, list):
        return [_model_to_dict(item) for item in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


# Backward-compat alias
_dataclass_to_dict = _model_to_dict


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
            # Don't overwrite subsystem dicts with scalar env values
            if config_key in data and isinstance(data[config_key], dict):
                continue
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


def _load_dotenv(dotenv_path: Path) -> None:
    """Load .env file into os.environ (simple key=value parser)."""
    if not dotenv_path.exists():
        return
    for line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = val


def _build_config_from_dict(data: Dict[str, Any]) -> SovereignConfig:
    """Build a SovereignConfig from a flat/nested dict via Pydantic validation."""
    # Pydantic handles nested model construction automatically.
    # We just need to ensure providers are in the right shape.
    providers_data = data.get("providers", [])
    clean_providers = []
    for p in providers_data:
        if isinstance(p, dict):
            clean_providers.append(p)
        elif isinstance(p, ProviderProfile):
            clean_providers.append(p.model_dump())
        elif isinstance(p, BaseModel):
            clean_providers.append(p.model_dump())
    data["providers"] = clean_providers

    return SovereignConfig.model_validate(data)


def load_config(
    config_path: Optional[str] = None,
    extra_overrides: Optional[Dict[str, Any]] = None,
) -> SovereignConfig:
    """
    Load configuration from file + .env + env vars + overrides.

    Priority: overrides > env vars > .env file > config file > defaults
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_FILE

    # Load .env if present (does not override existing env vars)
    dotenv = path.parent / ".env" if path.parent.exists() else DEFAULT_CONFIG_DIR / ".env"
    _load_dotenv(dotenv)

    # Start with defaults
    base = SovereignConfig().model_dump()

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
    data = config.model_dump()
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
