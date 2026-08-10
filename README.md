<p align="center">
  <img src="icon.svg" alt="Sovereign Claw" width="64" height="64" />
</p>

<h1 align="center">Sovereign Claw</h1>

<p align="center">
  <strong>A Sovereign Execution Kernel for AI Systems</strong>
</p>

<p align="center">
  <a href="https://github.com/BrewtaniusAI/sovereign-claw/actions/workflows/ci.yml"><img src="https://github.com/BrewtaniusAI/sovereign-claw/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <img src="https://img.shields.io/badge/coverage-90%25-brightgreen" alt="Coverage" />
  <img src="https://img.shields.io/badge/tests-468%2B-blue" alt="Tests" />
  <img src="https://img.shields.io/badge/version-3.2.0-orange" alt="Version" />
  <img src="https://img.shields.io/badge/python-3.10%20|%203.11%20|%203.12-blue" alt="Python" />
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="License" />
</p>

<p align="center">
  Deterministic, governed AI agent runtime with proof-vaulted execution, constraint-first governance, and fixed-time convergence. Every action is policy-gated, every decision is proof-vaulted, every drift is decomposed.
</p>

---

## Why Sovereign Claw

Most agent frameworks treat governance as an afterthought — logging what happened after it already happened. Sovereign Claw makes governance **the execution model itself**: policy projection before every action, bounded-time convergence, immutable audit trails, and deterministic refusal pathways.

This is not another LLM wrapper. This is a **governed runtime layer** your entire AI ecosystem sits on.

| Capability | Typical Agent Framework | Sovereign Claw v3.2 |
|---|---|---|
| Governance model | None (trust-the-model) | Constraint-first (ELFE, Proof Vault, drift control) |
| Execution guarantees | Probabilistic | Deterministic, bounded-time convergence |
| Audit trail | Logs | Immutable WORM ledger with exportable proof receipts + SHA-256 hash chains |
| Drift control | None | Decomposed: D(x) = D_tool + D_constraint + D_provider + D_policy |
| Policy engine | None | Adaptive profiles (strict/balanced/exploratory) + contextual drift rules + OPA/Rego |
| Refusal | Ad-hoc | First-class, tested refusal pathways (AG-07) |
| Multi-channel | Web only | 8 channels with cross-channel identity mesh + per-channel policies |
| Voice | Basic TTS | Multi-provider TTS/STT with failover chains |
| Browser | Puppeteer | Governed CDP with action audit trail |
| Skills | Plugin system | Signed skills with trust scores, permissions, evaluation harness (AG-02) |
| Agent orchestration | Single agent | Multi-agent federation (planner → executor → validator → critic) |
| MCP server | None | Full JSON-RPC 2.0 (stdio / SSE / WebSocket) |
| Model routing | Single provider | Economic router: multi-objective scoring, cost tracking, budget modes |
| Memory | None | Persistent SQLite-backed memory with TTL + relevance scoring |
| Observability | Basic logs | JSON structured logging with correlation IDs + trace context |
| Rate limiting | None | Token bucket per key/channel/provider + sliding window |
| Health checks | None | /health, /ready endpoints for container orchestration |
| Webhooks | Basic HTTP | HMAC-verified receivers with replay protection + dead letter queue |
| Event bus | None | Governed pub/sub with typed events + priority ordering |
| Automation | Cron | Cron + webhooks + interval + one-shot with ELFE convergence |

---

## Quick Start

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Bootstrap configuration + bundled skills
sovereign onboard

# System health check
sovereign doctor

# Run a governed objective
sovereign run "summarize the README" --provider demo

# Run with proof receipt output
sovereign run "analyze drift" --emit-receipt

# Preview without side effects
sovereign run "analyze drift" --preview --json
```

### Hello Governed World

The canonical end-to-end demo showing the full governance flow:

```bash
python examples/hello_governed_world.py
```

This walks through: **PolicyEngine gate → Orchestrator execution → ProofVault audit → Receipt export → Drift decomposition → Memory storage** — proving every action is governed.

---

## Architecture

```
User Intent
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│  PolicyEngine (adaptive governance gate)                     │
│  ├── Profiles: strict / balanced / exploratory               │
│  ├── Local rules (forbidden tools, payload limits, trace-id) │
│  ├── Contextual rules (drift-aware permission tightening)    │
│  ├── Learned signals (violation → deny pattern feedback)     │
│  └── Optional OPA/Rego external evaluation                   │
└────────────────────────┬────────────────────────────────────┘
                         │
    ┌────────────────────┼─────────────────────┐
    │                    │                      │
┌───▼────────┐   ┌──────▼───────┐   ┌─────────▼──────┐
│  Gateway   │   │ Orchestrator │   │   MCP Server   │
│ (WebSocket │   │ (ELFE core)  │   │  (JSON-RPC)    │
│  control   │   │              │   │                │
│  plane)    │   │  T_max bound │   │  Resources     │
│            │   │  Drift track │   │  Tools         │
│            │   │  Constraint  │   │  Prompts       │
│            │   │  projection  │   │  Sampling      │
└───┬────────┘   └──────┬───────┘   └────────────────┘
    │                    │
┌───▼────────┐   ┌──────▼───────────────────────┐
│  Channels  │   │  Multi-Agent Orchestrator     │
│  (8 gov'd  │   │  planner → executor →         │
│  interfaces│   │  validator → critic            │
│  + mesh)   │   │  (AG-05 role isolation)        │
└───┬────────┘   └──────┬───────────────────────┘
    │                    │
┌───▼────────┐   ┌──────▼───────┐   ┌──────────────┐
│  Memory    │   │ ModelRouter  │   │   Skills     │
│ (episodic  │   │ (economic +  │   │  (signed +   │
│  semantic  │   │  strategic   │   │  trust-scored │
│  task)     │   │  routing)    │   │  + AG-02)    │
└────────────┘   └──────┬───────┘   └──────────────┘
                        │
                 ┌──────▼───────┐
                 │  Proof Vault │
                 │ (WORM ledger │
                 │  + receipts  │
                 │  + replay    │
                 │  + diff)     │
                 └──────────────┘
```

### Core Governance Modules

| Module | File | What It Does |
|---|---|---|
| Orchestrator | `orchestrator.py` | ELFE-governed execution loop with drift tracking, constraint projection C(x), T_max enforcement, and risk threshold halting |
| Multi-Agent | `multi_agent.py` | Federated agent orchestrator with role registry (planner/executor/validator/critic), consensus-as-drift-minimization, AG-05 role isolation |
| Policy Engine | `policy_engine.py` | Adaptive governance with profiles (strict/balanced/exploratory), contextual drift-aware rules, learned violation signals, OPA/Rego |
| Proof Vault | `proof_vault.py` | Append-only WORM ledger with SHA-256 chained steps, Byzantine reputation weighting |
| Receipts | `receipts.py` | Exportable proof receipts (JSON/hash), hash chain verification, step-by-step replay, cross-run diff |
| Drift | `drift.py` | Decomposed drift: D_tool + D_constraint + D_provider + D_policy with cause tracking and breakdown reporting |
| Memory | `memory.py` | Episodic + semantic + task memory with TTL-based retention, relevance scoring, governed lifecycle |

### Execution Infrastructure

| Module | File | What It Does |
|---|---|---|
| Model Router | `model_router.py` | Multi-provider failover with circuit breakers, economic scoring (cost/latency/reputation/drift), budget-aware execution modes |
| Lanes | `lanes.py` | Tri-temporal routing: REFLEX → DELIBERATE → AUTHORITATIVE |
| Thermodynamics | `thermodynamics.py` | System energy/entropy tracking, TaskManifold constraint encoding, ELFE coefficient enforcement |
| Kitaev Shield | `kitaev_shield.py` | Topological error correction for agent state, sandboxed tool execution |
| Runtime | `runtime.py` | High-level execution interface wrapping orchestrator + proof vault, preview mode |
| Config | `config.py` | Pydantic v2-validated multi-source configuration (JSON + TOML + .env + env vars + field validators) |

### Platform Modules

| Module | File | What It Does |
|---|---|---|
| Gateway | `gateway.py` | WebSocket control plane with session lifecycle management, heartbeat, TLS |
| Channels | `channels/` | 8 messaging connectors: Discord, Slack, Telegram, WhatsApp, WebChat, IRC, Matrix, Signal |
| Channel Mesh | `channels/mesh.py` | Cross-channel identity linking, session continuity across channels, per-channel policy overrides |
| Skills | `skills.py` | Signed skill management with trust scores, permission scoping, violation tracking, evaluation harness (AG-02) |
| Security | `security.py` | DM pairing, allowlists/denylists, secret detection, reputation tracking, rate limiting |
| Browser | `browser.py` | Governed CDP browser automation with timeout-bounded actions and audit trail |
| Voice | `voice.py` | Multi-provider TTS/STT with failover chains (ElevenLabs, OpenAI, Whisper, Deepgram) |
| Canvas | `canvas.py` | FSM-governed live canvas with element management, snapshots, render timeout |
| Sessions | `sessions.py` | Agent-to-agent sessions with AG-05 role isolation and containment |
| Scheduler | `scheduler.py` | Cron/webhook/interval/one-shot automation with ELFE convergence guarantees |
| MCP Server | `mcp_server.py` | Model Context Protocol server (JSON-RPC 2.0) with resources, tools, prompts, sampling |
| Web UI | `web_ui.py` | Operator console backend with real-time drift monitoring (React frontend in `web/`) |

### Safety & Interop

| Module | File | What It Does |
|---|---|---|
| A2A Protocol | `a2a.py` | Agent2Agent interop: agent cards, task lifecycle (SUBMITTED→WORKING→COMPLETED/FAILED/CANCELED), opaque collaboration |
| Guardrails | `guardrails.py` | Autonomous safety constraints: privilege escalation prevention, loop detection, destructive action gating, cost/token limits |
| Persistent Memory | `persistent_memory.py` | SQLite-backed episodic/semantic/task memory with TTL, relevance scoring, capacity enforcement |

### Production Infrastructure (v3.2.0)

| Module | File | What It Does |
|---|---|---|
| Structured Logging | `structured_logging.py` | JSON-formatted logging with correlation IDs, trace context propagation, configurable formatters |
| Rate Limiter | `rate_limiter.py` | Token bucket rate limiting: per-key/per-channel/per-provider, sliding window, burst detection |
| Health Check | `health.py` | Container orchestration endpoints: /health, /ready, component status, dependency verification |
| Webhook Receiver | `webhooks.py` | HMAC-SHA256 signature verification, event routing, replay protection, dead letter queue |
| Event Bus | `event_bus.py` | Governed pub/sub: typed events, priority ordering, dead letter queue, event history |

### Additional Modules

| Module | File | What It Does |
|---|---|---|
| Weavers Kernel | `weavers_kernel.py` | Human-in-the-loop skill leveling with ELFE convergence |
| Mythic Neuro Kernel | `mythic_neuro_kernel.py` | Mathematical skill transition engine with Dongba glyphs |
| Gardeners Protocol | `gardeners_protocol.py` | Persistence layer for skill ledgers (scrolls) with event logging |
| Graph ELVE | `graph_elve.py` | LangGraph workflow orchestration |
| Event Stream | `event_stream.py` | Typed event stream with structured records |
| IP Shield | `ip_shield.py` | Build fingerprinting and ELFE coefficient loading |
| Tools Basic | `tools_basic.py` | ToolSpec dataclass and basic tool registry |

---

## Drift Model

Sovereign Claw decomposes drift into actionable, debuggable components:

```
D_total(x) = D_tool + D_constraint + D_provider + D_policy

Where:
  D_tool       = drift from tool execution errors or penalties
  D_constraint = drift from constraint projection mismatches
  D_provider   = drift from provider failures or latency
  D_policy     = drift from policy violations or tightening
```

Each component is tracked independently per execution step, enabling targeted debugging and optimization. The `DriftTracker` identifies the dominant drift source and provides a full breakdown report.

```bash
# View drift breakdown for a trace
sovereign drift <trace-id>
```

---

## Proof Receipts

Every execution produces a verifiable proof receipt — a SHA-256 hash chain proving exactly what happened, in what order, with what drift.

```bash
# Export receipt as JSON
sovereign run "task" --emit-receipt

# Inspect a trace
sovereign trace <trace-id>

# Replay step-by-step
sovereign replay <trace-id>
```

Receipts support:
- **Export** as JSON or hash digest
- **Hash chain verification** — tamper-evident chain of custody
- **Step-by-step replay** — reconstruct execution with drift deltas
- **Cross-run diff** — compare two executions side-by-side

---

## Multi-Agent Orchestration

The multi-agent orchestrator enforces AG-05 role isolation: no agent can plan + execute + validate simultaneously.

```
planner → executor → validator → policy check → critic → finalize
```

- **Agent Registry** — register agents with explicit roles
- **Role isolation** — planner, executor, validator, critic as separate entities
- **Consensus = drift minimization** — inter-agent disagreement spikes drift
- **Byzantine reputation** — agents earn trust through consistent behavior

---

## Adaptive Policy Engine

The policy engine supports three governance profiles that control enforcement strictness:

| Profile | Max Payload | Require Trace ID | Use Case |
|---|---|---|---|
| `strict` | 16 KB | Yes | Production, audited environments |
| `balanced` | 32 KB | No | Standard development |
| `exploratory` | 64 KB | No | Research, experimentation |

Contextual rules tighten permissions automatically when drift exceeds thresholds. Learned signals feed violations back into deny patterns.

```bash
# Test policy against sample input
sovereign policy test

# Run with specific profile
sovereign run "task" --policy-profile strict
```

---

## Economic Model Router

The model router scores providers using multi-objective optimization:

```
Score = w1*(success_rate) + w2*(1/latency) + w3*(reputation)
      - w4*(cost_per_token) - w5*(drift_penalty)
```

Features:
- **8 provider backends** — Anthropic, OpenAI, Gemini, Perplexity, Groq, Mistral, Ollama, Local
- **Circuit breakers** — per-provider fault isolation
- **Budget-aware execution** — low-cost mode / high-accuracy mode
- **Cost tracking** — per-call cost recording and reporting

```bash
# List providers with stats
sovereign providers

# Run with budget constraint
sovereign run "task" --budget 0.50
```

---

## CLI Command Center

```bash
# Execution
sovereign run <objective>          # Execute governed objective
sovereign run <obj> --emit-receipt # With proof receipt output
sovereign run <obj> --preview      # Dry-run without side effects

# Setup & Diagnostics
sovereign onboard                  # Bootstrap config + install skills
sovereign doctor                   # System health diagnostics

# Inspection
sovereign trace <id>               # Inspect execution trace
sovereign replay <id>              # Replay execution step-by-step
sovereign drift <id>               # Show drift breakdown for trace
sovereign providers                # List providers with stats
sovereign policy test              # Test policy against sample input
sovereign memory                   # Show memory stats

# Configuration
sovereign gateway                  # Show gateway configuration
sovereign skills                   # List installed skills
sovereign config                   # View current configuration
sovereign version                  # Print version
```

### Flags

| Flag | Description |
|---|---|
| `--provider` | Backend: `demo` (dev-only), `ollama`, `giles`, or configured providers |
| `--json` | Raw JSON output |
| `--preview` | Dry-run without side effects |
| `--forbid` | Forbidden actions (repeatable) |
| `--t-max` | Maximum execution steps |
| `--risk-threshold` | Soft halt threshold (0.0–1.0) |
| `--emit-receipt` | Output proof receipt after execution |
| `--policy-profile` | Policy profile: `strict`, `balanced`, `exploratory` |
| `--budget` | Max cost budget for execution (USD) |

---

## Channels

8 governed messaging interfaces, all gated by PolicyEngine:

| Channel | Connector | Status |
|---|---|---|
| Discord | `DiscordConnector` | Production |
| Slack | `SlackConnector` | Production |
| Telegram | `TelegramConnector` | Production |
| WhatsApp | `WhatsAppConnector` | Production |
| WebChat | `WebChatConnector` | Production |
| IRC | `IRCConnector` | Production |
| Matrix | `MatrixConnector` | Production |
| Signal | `SignalConnector` | Production |

The **Channel Mesh** (`channels/mesh.py`) links identities across channels, maintains session continuity, and applies per-channel policy overrides (e.g., Slack → strict mode, Discord → balanced mode).

---

## Memory Layer

```
Memory
├── Episodic Memory   — timestamped event records from execution traces
├── Semantic Memory   — extracted knowledge with relevance scoring
└── Task Memory       — objective-specific context with TTL-based retention

Governed by:
  - Retention policies (max entries, TTL expiry)
  - Relevance scoring for retrieval
  - ProofVault integration for audit trail
```

```bash
sovereign memory   # Show memory stats
```

---

## Docker

```bash
# Build and run the authenticated bridge/operator console
export SOVEREIGN_BRIDGE_TOKEN=change-me
docker compose up -d

# Readiness / health
curl http://127.0.0.1:8787/ready
curl http://127.0.0.1:8787/health

# Authenticated preview request
AUTH_HEADER="$(printf '%s %s' "${BEARER_PREFIX:-Bearer}" "$SOVEREIGN_BRIDGE_TOKEN")"
curl -H "Authorization: ${AUTH_HEADER}" \
     -H "Content-Type: application/json" \
     -d '{"objective":"system check then run governed","intent":"preview"}' \
     http://127.0.0.1:8787/preview
```

---

## Configuration

Configuration is loaded from (highest priority first):
1. Runtime overrides (CLI flags)
2. Environment variables (`SOVEREIGN_*`)
3. Config file (`~/.sovereign_claw/config.json`)
4. Defaults

```bash
# View current config
sovereign config --json

# Edit config file
$EDITOR ~/.sovereign_claw/config.json
```

---

## Governance Guarantees

Sovereign Claw enforces the **Isomorphic Closure Invariant** (God File v∞.1):

| # | Guarantee | Implementation |
|---|---|---|
| 1 | **Fixed-time convergence** | ELFE v∞.1 guarantees drift → 0 within bounded T_max (no asymptotic tails) |
| 2 | **Constraint closure** | All constraints form closed entailment loops via PolicyEngine + C(x) projection |
| 3 | **Proof-backed auditability** | Every decision recorded to WORM ledger with exportable receipts |
| 4 | **Adaptive policy gating** | PolicyEngine evaluates all messages with profile-aware rules |
| 5 | **Refusal as capability** | First-class, tested, deterministic refusal pathways (AG-07) |
| 6 | **Agent mortality** | No immortal agents, no trans-repo identity (AG-03) |
| 7 | **Evaluation before authority** | No output without passing eval harness (AG-02) |
| 8 | **Role isolation** | No agent can plan + execute + validate simultaneously (AG-05) |
| 9 | **Repository-bound intelligence** | Agent identity constrained to single versioned repo (AG-01) |
| 10 | **Tool sovereignty** | Explicit tool declaration, sandboxed execution, pre/post conditions (AG-04) |
| 11 | **Non-proliferation** | Fork hazard classification, clean-room mirror rule (AG-06) |
| 12 | **Decomposed drift** | D(x) = D_tool + D_constraint + D_provider + D_policy |
| 13 | **Governed memory** | Episodic/semantic/task memory with retention policies and TTL |
| 14 | **Structured observability** | JSON structured logging with correlation IDs and trace context |
| 15 | **Rate governance** | Token bucket rate limiting per key, channel, provider, and tool |
| 16 | **Health probes** | Liveness and readiness checks for container orchestration |
| 17 | **Webhook integrity** | HMAC signature verification with replay protection |
| 18 | **Governed events** | Pub/sub event bus with dead letter queue and audit trail |

---

## Development

```bash
make lint        # ruff check
make fmt         # ruff format
make typecheck   # mypy strict
make test        # pytest (468+ tests)
make coverage    # pytest --cov (≥85% required)
make package     # build wheel
make sbom        # generate SBOM
```

CI runs on every push: lint → format → mypy → tests (Python 3.10/3.11/3.12) → coverage → SBOM.

---

## Examples

| Example | Description |
|---|---|
| `hello_governed_world.py` | Canonical E2E demo: policy gate → execution → proof receipt → drift decomposition |
| `00_quickstart.py` | Minimal quickstart |
| `01_safe_web_scrape.py` | Governed web scraping |
| `02_multi_agent_dag.py` | Multi-agent DAG execution |
| `03_langgraph_elve_loop.py` | LangGraph ELFE loop |
| `04_kitaev_penalty_tiers.py` | Kitaev penalty tier demonstration |
| `05_full_swarm_demo.py` | Full swarm execution |

---

## Project Structure

```
sovereign-claw/
├── src/sovereign_claw/
│   ├── __init__.py              # v3.2.0 exports + lazy imports
│   ├── orchestrator.py          # ELFE execution loop
│   ├── multi_agent.py           # Federated agent orchestrator
│   ├── runtime.py               # High-level runtime
│   ├── proof_vault.py           # WORM audit ledger
│   ├── receipts.py              # Proof receipts + hash chain + replay + diff
│   ├── policy_engine.py         # Adaptive governance gate
│   ├── drift.py                 # Decomposed drift tracking
│   ├── memory.py                # Episodic + semantic + task memory
│   ├── model_router.py          # Economic multi-provider routing
│   ├── config.py                # Pydantic v2 configuration
│   ├── lanes.py                 # Tri-temporal routing
│   ├── thermodynamics.py        # System energy/entropy + TaskManifold
│   ├── kitaev_shield.py         # Topological error correction
│   ├── gateway.py               # WebSocket control plane
│   ├── channels/                # Multi-channel connectors
│   │   ├── base.py              # Abstract channel protocol
│   │   ├── connectors.py        # 8 concrete implementations
│   │   └── mesh.py              # Cross-channel identity + session continuity
│   ├── skills.py                # Signed skills platform
│   ├── security.py              # Access control + reputation
│   ├── browser.py               # CDP browser automation
│   ├── voice.py                 # TTS/STT engine
│   ├── canvas.py                # Live visual canvas
│   ├── sessions.py              # A2A agent sessions
│   ├── scheduler.py             # Cron/webhook automation
│   ├── mcp_server.py            # MCP server (JSON-RPC 2.0)
│   ├── web_ui.py                # Operator console backend
│   ├── cli.py                   # Command-line interface
│   ├── weavers_kernel.py        # Skill leveling engine
│   ├── mythic_neuro_kernel.py   # Mathematical skill transitions
│   ├── gardeners_protocol.py    # Skill scroll persistence
│   ├── graph_elve.py            # LangGraph orchestration
│   ├── event_stream.py          # Typed event stream
│   ├── ip_shield.py             # Build fingerprint
│   ├── tools_basic.py           # Basic tool registry
│   ├── backends_giles.py        # Giles tiered backend
│   └── backends_ollama.py       # Ollama backend
├── web/                         # React + Vite + Tailwind operator console
├── tests/                       # 468+ tests across 15+ test files
├── examples/                    # 7 runnable demos
│   └── hello_governed_world.py  # Canonical E2E governance demo
├── Dockerfile                   # Production container
├── docker-compose.yml           # Compose with sandbox profile
├── ARCHITECTURE.md              # Module → capability → status map
├── VISION.md                    # Project vision and roadmap
├── SECURITY.md                  # Security model documentation
├── CONTRIBUTING.md              # Contribution guidelines
│   ├── structured_logging.py    # JSON structured logging
│   ├── rate_limiter.py          # Token bucket rate limiting
│   ├── health.py                # Health check API
│   ├── webhooks.py              # Webhook receiver
│   ├── event_bus.py             # Governed event bus
│   ├── a2a.py                   # Agent2Agent protocol
│   ├── guardrails.py            # Autonomous guardrails
│   ├── persistent_memory.py     # SQLite persistent memory
│   ├── backends_giles.py        # Giles tiered backend
│   └── backends_ollama.py       # Ollama backend
├── web/                         # React + Vite + Tailwind operator console
├── tests/                       # 468+ tests
├── examples/                    # 7 runnable demos
│   └── hello_governed_world.py  # Canonical E2E governance demo
├── Dockerfile                   # Production container (non-root)
├── docker-compose.yml           # Compose with sandbox profile
├── ARCHITECTURE.md              # Module → capability → status map
├── VISION.md                    # Project vision and roadmap
├── SECURITY.md                  # Security model documentation
├── CONTRIBUTING.md              # Contribution guidelines
└── pyproject.toml               # Build configuration (v3.2.0)
```

---

## Testing

```bash
# Run the full test suite
pytest -q

# Expected output:
# 151+ passed
```

Coverage target: **≥ 85%** (currently 90%+).

---

## Dashboard

Sovereign Claw includes an AI-integrated **Liquid Glass** dashboard providing a visual interface for:

- Governance pipeline monitoring (QC → GATA → GATA PRIME flow)
- Proof Vault trace inspection and replay
- Lane routing visualization (Rabbit → Cypher → Giles)
- ELFE drift metrics and convergence tracking
- AI Auditor chat with governance-aware contextual responses
- Command palette (`Ctrl+K`) with fuzzy search
- EU AI Act transparency labels on AI-generated content

> **Note:** The dashboard is available on the [`devin/1775154432-ai-dashboard`](https://github.com/BrewtaniusAI/sovereign-claw/tree/devin/1775154432-ai-dashboard) branch. Once merged, open `dashboard/index.html` in any browser.

---

## CollectiveOS Integration

Sovereign Claw is the operator console and agent orchestration layer within the CollectiveOS ecosystem:

| Integration | Role |
|------------|------|
| **QC Gate** | Self-audit before significant actions |
| **GATA** | Sandboxed testing and edge-case validation |
| **GATA PRIME** | Formal verification and audit trail maintenance |
| **Proof Vault** | WORM (Write Once Read Many) receipt logging |
| **ELFE Kernel** | Fixed-time convergence stability guarantees |
| **Constraint Engine** | Shared drift measurement and enforcement patterns |
| **PAT** | Pan-African language infrastructure integration |
| **SFO App** | Governed API gateway integration |
| **Sentinel Engine** | Bevy ECS runtime for governed simulations |
| **AION Holodeck** | Temporal simulation and execution path analysis |

---

## Further Reading

- [ARCHITECTURE.md](ARCHITECTURE.md) — Authoritative module → capability → status map
- [VISION.md](VISION.md) — Project vision and roadmap
- [SECURITY.md](SECURITY.md) — Security model and threat surface
- [CONTRIBUTING.md](CONTRIBUTING.md) — How to contribute

---
## License

Apache-2.0
