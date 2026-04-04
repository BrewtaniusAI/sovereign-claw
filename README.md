# sovereign-claw

[![CI](https://github.com/BrewtaniusAI/sovereign-claw/actions/workflows/ci.yml/badge.svg)](https://github.com/BrewtaniusAI/sovereign-claw/actions/workflows/ci.yml)
![Coverage](https://img.shields.io/badge/coverage-93%25-brightgreen)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)
![Version](https://img.shields.io/badge/version-3.0.0-orange)

**Governed sovereign agent runtime** with proof-vaulted execution, constraint-first governance, multi-channel messaging, MCP server, skills platform, voice engine, live canvas, agent-to-agent sessions, and browser automation — all governed by the Isomorphic Closure Invariant and ELFE v∞.1 fixed-time convergence.

---

## What Makes Sovereign Claw Different

| Capability | OpenClaw | Sovereign Claw v3 |
|---|---|---|
| Governance model | None (trust-the-model) | Constraint-first (ELFE, Proof Vault, drift control) |
| Execution guarantees | Probabilistic | Deterministic, bounded-time convergence |
| Audit trail | Logs | Immutable Proof Vault with exportable receipts + hash chains |
| Drift control | None | Decomposed D(x) = D_tool + D_constraint + D_provider + D_policy |
| Policy engine | None | Adaptive PolicyEngine with profiles (strict/balanced/exploratory) + OPA/Rego |
| Refusal capability | Ad-hoc | First-class, tested refusal pathways (AG-07) |
| Multi-channel | Web only | 8 channels with cross-channel identity + per-channel policies |
| Voice | Basic TTS | Multi-provider TTS/STT with failover chains |
| Browser control | Puppeteer | Governed CDP with action audit trail |
| Skills platform | Plugin system | Signed skills with trust scores, permissions, evaluation harness (AG-02) |
| Agent sessions | Single agent | Multi-agent orchestrator (planner→executor→validator→critic) |
| MCP server | None | Full JSON-RPC 2.0 (stdio/SSE/WebSocket) |
| Automation | Cron | Cron + webhooks + interval + one-shot with ELFE convergence |
| Model routing | Single provider | Economic router: cost tracking, budget modes, multi-objective scoring |
| Memory | None | Episodic + semantic + task memory with governed retention |
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

# Run with proof receipt output
sovereign run "analyze drift" --emit-receipt

# Preview without side effects
sovereign run "analyze drift" --preview --json
```

---

## Architecture

```
                    ┌─────────────────────────────────┐
                    │         PolicyEngine             │
                    │  (adaptive governance gate)      │
                    │  profiles: strict/balanced/expl   │
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
   │ (8 governed │    │ (economic +   │    │ (signed +     │
   │  interfaces)│    │  strategic)   │    │  trust-scored)│
   └──────┬──────┘    └───────┬───────┘    └───────────────┘
          │                   │
   ┌──────▼──────┐    ┌──────▼────────┐
   │   Memory    │    │  Proof Vault  │
   │ (episodic + │    │ (WORM ledger +│
   │  semantic)  │    │  receipts)    │
   └─────────────┘    └──────────────┘
```

### Core Modules

| Module | Purpose | Status |
|---|---|---|
| `orchestrator.py` | ELFE-governed execution loop with drift tracking | Production |
| `multi_agent.py` | Federated agent orchestrator (planner→executor→validator→critic) | Production |
| `runtime.py` | High-level runtime wrapping orchestrator + proof vault | Production |
| `proof_vault.py` | Append-only WORM ledger with SHA-256 chained steps | Production |
| `receipts.py` | Exportable proof receipts with hash chains, replay, diff | Production |
| `policy_engine.py` | Adaptive governance with profiles + contextual rules + OPA/Rego | Production |
| `lanes.py` | Tri-temporal routing: REFLEX → DELIBERATE → AUTHORITATIVE | Production |
| `drift.py` | Decomposed drift tracking (D_tool + D_constraint + D_provider + D_policy) | Production |
| `thermodynamics.py` | System energy/entropy tracking | Production |
| `kitaev_shield.py` | Topological error correction for agent state | Production |
| `memory.py` | Episodic + semantic + task memory with governed retention | Production |

### v3.0.0 Platform Modules

| Module | Purpose | Status |
|---|---|---|
| `config.py` | Dataclass-based configuration (JSON + env vars + overrides) | Production |
| `model_router.py` | Economic multi-provider routing with cost tracking + budget modes | Production |
| `gateway.py` | WebSocket control plane with session management | Production |
| `channels/` | Multi-channel messaging (8 connectors) with cross-channel identity | Production |
| `skills.py` | Signed skill management with trust scores + permission scoping | Production |
| `security.py` | DM pairing, allowlists, secret detection, reputation | Production |
| `browser.py` | Governed CDP browser automation | Production |
| `voice.py` | Multi-provider TTS/STT with failover | Production |
| `canvas.py` | FSM-governed live canvas with snapshots | Production |
| `sessions.py` | A2A agent sessions with AG-05 role isolation | Production |
| `scheduler.py` | Cron/webhook/interval automation with ELFE convergence | Production |
| `mcp_server.py` | Model Context Protocol server (JSON-RPC 2.0) | Production |

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
sovereign trace <id>          # Inspect execution trace
sovereign replay <id>         # Replay execution step-by-step
sovereign drift <id>          # Show drift breakdown for trace
sovereign providers           # List providers with stats
sovereign policy test         # Test policy against sample input
sovereign memory              # Show memory stats
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
3. **Proof Vault** — Every decision recorded to immutable WORM ledger with exportable receipts
4. **Adaptive policy gating** — PolicyEngine evaluates all inbound messages with profile-aware rules
5. **Refusal as capability** — First-class, tested refusal pathways (AG-07)
6. **Agent mortality** — No immortal agents, no trans-repo identity (AG-03)
7. **Evaluation before authority** — No output without passing eval harness (AG-02)
8. **Role isolation** — No agent can plan + execute + validate simultaneously (AG-05)
9. **Decomposed drift** — D(x) = D_tool + D_constraint + D_provider + D_policy for full observability
10. **Governed memory** — Episodic/semantic/task memory with retention policies

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
│   ├── multi_agent.py       # Federated agent orchestrator
│   ├── runtime.py           # High-level runtime
│   ├── proof_vault.py       # WORM audit ledger
│   ├── receipts.py          # Proof receipt export + hash chain + replay + diff
│   ├── policy_engine.py     # Adaptive governance gate
│   ├── drift.py             # Decomposed drift tracking
│   ├── memory.py            # Episodic + semantic + task memory
│   ├── lanes.py             # Tri-temporal routing
│   ├── config.py            # Dataclass-based configuration
│   ├── model_router.py      # Economic multi-provider routing
│   ├── gateway.py           # WebSocket control plane
│   ├── channels/            # Multi-channel connectors
│   │   ├── base.py          # Abstract channel protocol
│   │   ├── connectors.py    # 8 concrete implementations
│   │   └── mesh.py          # Cross-channel identity + session continuity
│   ├── skills.py            # Signed skills platform
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
│   └── hello_governed_world.py
├── Dockerfile               # Production container
├── docker-compose.yml       # Compose with sandbox
├── ARCHITECTURE.md          # Module → capability → status map
└── pyproject.toml           # Build configuration
```

---

## License

Apache-2.0
