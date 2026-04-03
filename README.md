# sovereign-claw

[![CI](https://github.com/BrewtaniusAI/sovereign-claw/actions/workflows/ci.yml/badge.svg)](https://github.com/BrewtaniusAI/sovereign-claw/actions/workflows/ci.yml)
![Coverage](https://img.shields.io/badge/coverage-93%25-brightgreen)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Version](https://img.shields.io/badge/version-3.0.0-orange)

**Deterministic, thermodynamically governed AI agent platform** with proof-vaulted execution, constraint-first governance, multi-channel messaging, MCP server, skills platform, voice engine, live canvas, agent-to-agent sessions, and browser automation — all governed by the Isomorphic Closure Invariant and ELFE v∞.1 fixed-time convergence.

---

## What Makes Sovereign Claw Different

| Capability | OpenClaw | Sovereign Claw v3 |
|---|---|---|
| Governance model | None (trust-the-model) | Constraint-first (ELFE, Proof Vault, drift control) |
| Execution guarantees | Probabilistic | Deterministic, bounded-time convergence |
| Audit trail | Logs | Immutable Proof Vault (SHA-256 chained WORM ledger) |
| Drift control | None | Real-time D(x) = \|\|x - C(x)\|\| tracking |
| Policy engine | None | PolicyEngine with local + OPA/Rego support |
| Refusal capability | Ad-hoc | First-class, tested refusal pathways (AG-07) |
| Multi-channel | Web only | Discord, Slack, Telegram, WhatsApp, WebChat, IRC, Matrix, Signal |
| Voice | Basic TTS | Multi-provider TTS/STT with failover chains |
| Browser control | Puppeteer | Governed CDP with action audit trail |
| Skills platform | Plugin system | Bundled/managed/workspace with evaluation harness (AG-02) |
| Agent sessions | Single agent | A2A protocol with role isolation (AG-05) |
| MCP server | None | Full JSON-RPC 2.0 (stdio/SSE/WebSocket) |
| Automation | Cron | Cron + webhooks + interval + one-shot with ELFE convergence |
| Model routing | Single provider | Multi-provider failover with circuit breakers + reputation |
| Docker | Basic | Production Dockerfile + compose with sandbox profile |

---

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Bootstrap configuration + bundled skills
sovereign onboard

# System health check
sovereign doctor

# Run a governed objective
sovereign run "summarize the README" --provider demo

# Preview without side effects
sovereign run "analyze drift" --preview --json
```

---

## Architecture

```
                    ┌─────────────────────────────────┐
                    │         PolicyEngine             │
                    │   (governance gate for all I/O)  │
                    └──────────┬──────────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │                    │                     │
   ┌──────▼──────┐    ┌───────▼───────┐    ┌───────▼───────┐
   │   Gateway    │    │  Orchestrator │    │   MCP Server  │
   │  (WebSocket) │    │  (ELFE core)  │    │  (JSON-RPC)   │
   └──────┬───────┘    └───────┬───────┘    └───────┬───────┘
          │                    │                     │
   ┌──────▼──────┐    ┌───────▼───────┐    ┌───────▼───────┐
   │  Channels   │    │  ModelRouter  │    │    Skills     │
   │ (8 connectors)   │ (multi-provider│    │  (eval gated) │
   └─────────────┘    │  + failover)  │    └───────────────┘
                      └───────┬───────┘
                              │
                    ┌─────────▼─────────┐
                    │    Proof Vault     │
                    │ (WORM audit ledger)│
                    └───────────────────┘
```

### Core Modules

| Module | Purpose |
|---|---|
| `orchestrator.py` | ELFE-governed execution loop with drift tracking |
| `runtime.py` | High-level runtime wrapping orchestrator + proof vault |
| `proof_vault.py` | Append-only WORM ledger with SHA-256 chained steps |
| `policy_engine.py` | Governance gating for all inbound messages |
| `lanes.py` | Tri-temporal routing: REFLEX → DELIBERATE → AUTHORITATIVE |
| `thermodynamics.py` | System energy/entropy tracking |
| `kitaev_shield.py` | Topological error correction for agent state |

### v3.0.0 Platform Modules

| Module | Purpose |
|---|---|
| `config.py` | Unified configuration (JSON + env vars + overrides) |
| `model_router.py` | Multi-provider LLM routing with circuit breakers |
| `gateway.py` | WebSocket control plane with session management |
| `channels/` | Multi-channel messaging (8 connectors) |
| `skills.py` | Skill management with AG-02 evaluation harness |
| `security.py` | DM pairing, allowlists, secret detection, reputation |
| `browser.py` | Governed CDP browser automation |
| `voice.py` | Multi-provider TTS/STT with failover |
| `canvas.py` | FSM-governed live canvas with snapshots |
| `sessions.py` | A2A agent sessions with AG-05 role isolation |
| `scheduler.py` | Cron/webhook/interval automation with ELFE convergence |
| `mcp_server.py` | Model Context Protocol server (JSON-RPC 2.0) |

---

## CLI Commands

```bash
sovereign run <objective>     # Execute governed objective
sovereign onboard             # Bootstrap config + install skills
sovereign doctor              # System health diagnostics
sovereign gateway             # Show gateway configuration
sovereign skills              # List installed skills
sovereign config              # View current configuration
sovereign version             # Print version
```

### Flags

| Flag | Description |
|---|---|
| `--provider` | Backend: `demo`, `ollama`, `giles` |
| `--json` | Raw JSON output |
| `--preview` | Dry-run without side effects |
| `--forbid` | Forbidden actions (repeatable) |
| `--t-max` | Maximum execution steps |
| `--risk-threshold` | Soft halt threshold (0.0–1.0) |

---

## Docker

```bash
# Build and run
docker compose up -d

# Run sandbox (isolated execution)
docker compose --profile sandbox up sovereign-sandbox

# Health check
docker compose exec sovereign sovereign doctor
```

---

## Configuration

Configuration is loaded from (highest priority first):
1. Runtime overrides
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

1. **Fixed-time convergence** — ELFE v∞.1 guarantees drift → 0 within bounded T_max (no asymptotic tails)
2. **Constraint closure** — All constraints form closed entailment loops
3. **Proof Vault** — Every decision recorded to immutable WORM ledger
4. **Policy gating** — PolicyEngine evaluates all inbound messages
5. **Refusal as capability** — First-class, tested refusal pathways (AG-07)
6. **Agent mortality** — No immortal agents, no trans-repo identity (AG-03)
7. **Evaluation before authority** — No output without passing eval harness (AG-02)
8. **Role isolation** — No agent can plan + execute + validate simultaneously (AG-05)

---

## Development

```bash
make lint        # ruff check
make typecheck   # mypy strict
make test        # pytest
make coverage    # pytest --cov (≥85% required)
make package     # build wheel
make sbom        # generate SBOM
```

---

## Project Structure

```
sovereign-claw/
├── src/sovereign_claw/
│   ├── __init__.py          # v3.0.0 exports + lazy imports
│   ├── orchestrator.py      # ELFE execution loop
│   ├── runtime.py           # High-level runtime
│   ├── proof_vault.py       # WORM audit ledger
│   ├── policy_engine.py     # Governance gate
│   ├── lanes.py             # Tri-temporal routing
│   ├── config.py            # Configuration system
│   ├── model_router.py      # Multi-provider routing
│   ├── gateway.py           # WebSocket control plane
│   ├── channels/            # Multi-channel connectors
│   │   ├── base.py          # Abstract channel protocol
│   │   └── connectors.py    # 8 concrete implementations
│   ├── skills.py            # Skills platform
│   ├── security.py          # Access control + reputation
│   ├── browser.py           # CDP browser automation
│   ├── voice.py             # TTS/STT engine
│   ├── canvas.py            # Live visual canvas
│   ├── sessions.py          # A2A agent sessions
│   ├── scheduler.py         # Cron/webhook automation
│   ├── mcp_server.py        # MCP server
│   └── cli.py               # Command-line interface
├── tests/                   # Full test suite
├── examples/                # Runnable demos
├── Dockerfile               # Production container
├── docker-compose.yml       # Compose with sandbox
└── pyproject.toml           # Build configuration
```

---

## License

Apache-2.0
