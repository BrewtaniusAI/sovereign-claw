# Sovereign Claw — Architecture Map

> Authoritative module → capability → status reference.
> Updated for v3.1.0.

---

## Governance Flow

```
User Intent
    │
    ▼
PolicyEngine (adaptive profiles: strict / balanced / exploratory)
    │
    ├── Local rules (forbidden tools, payload limits, trace-id)
    ├── Contextual rules (drift-aware permission tightening)
    ├── Learned signals (violation → deny pattern feedback)
    └── Optional OPA/Rego external evaluation
    │
    ▼
Orchestrator (ELFE fixed-time convergence loop)
    │
    ├── Constraint projection C(x) before every action
    ├── Drift tracking: D(x) = D_tool + D_constraint + D_provider + D_policy
    ├── Forbidden-action hard block
    ├── T_max enforcement (hard silence clause)
    ├── Risk threshold enforcement (soft silence clause)
    └── Byzantine reputation weighting per agent
    │
    ▼
Multi-Agent Orchestrator (optional)
    │
    ├── Agent Registry (planner, executor, validator, critic)
    ├── Role isolation (AG-05: no plan+execute+validate in same lane)
    ├── Inter-agent disagreement → drift spike
    └── Consensus = drift minimization
    │
    ▼
ModelRouter (economic, multi-provider)
    │
    ├── Priority-weighted failover chain
    ├── Circuit breaker per provider
    ├── Multi-objective scoring: success_rate, latency, reputation, cost, drift
    ├── Budget-aware execution modes (low-cost / high-accuracy)
    └── Cost tracking per call
    │
    ▼
Tool Execution (via Kitaev Shield)
    │
    ├── Sandboxed execution
    ├── Drift penalty computation
    └── Error containment
    │
    ▼
Proof Vault + Receipts
    │
    ├── WORM ledger (SQLite, SHA-256 chained)
    ├── Exportable receipts (JSON / hash digest)
    ├── Step-by-step replay
    ├── Diff between runs
    └── Agent reputation tracking
```

---

## Module Map

### Core Governance (Production)

| Module | File | Capability | Status |
|---|---|---|---|
| Orchestrator | `orchestrator.py` | ELFE-governed execution loop, drift tracking, constraint projection, T_max/risk enforcement | Production |
| Multi-Agent | `multi_agent.py` | Federated agent orchestrator with role registry (planner/executor/validator/critic), consensus-as-drift-minimization | Production |
| Policy Engine | `policy_engine.py` | Adaptive governance with profiles (strict/balanced/exploratory), contextual drift-aware rules, OPA/Rego, learned violation signals | Production |
| Proof Vault | `proof_vault.py` | Append-only WORM ledger, SHA-256 chained steps, Byzantine reputation weighting | Production |
| Receipts | `receipts.py` | Exportable proof receipts (JSON/hash), hash chain verification, step-by-step replay, cross-run diff | Production |
| Drift | `drift.py` | Decomposed drift: D_tool + D_constraint + D_provider + D_policy, cause tracking, breakdown reporting | Production |
| Memory | `memory.py` | Episodic + semantic + task memory with governed retention policies, TTL, relevance scoring | Production |
| Runtime | `runtime.py` | High-level execution interface wrapping orchestrator + proof vault, preview mode | Production |

### Execution Infrastructure (Production)

| Module | File | Capability | Status |
|---|---|---|---|
| Model Router | `model_router.py` | Multi-provider failover, circuit breakers, economic scoring (cost/latency/reputation/drift), budget modes | Production |
| Lanes | `lanes.py` | Tri-temporal routing: REFLEX → DELIBERATE → AUTHORITATIVE | Production |
| Thermodynamics | `thermodynamics.py` | System energy/entropy tracking, ELFE coefficient enforcement | Production |
| Kitaev Shield | `kitaev_shield.py` | Topological error correction for agent state, sandboxed tool execution | Production |
| Config | `config.py` | Pydantic v2-validated multi-source configuration (JSON + TOML + .env + env vars + field validators) | Production |

### Safety & Interop (Production)

| Module | File | Capability | Status |
|---|---|---|---|
| A2A Protocol | `a2a.py` | Agent2Agent interop: agent cards, task lifecycle (SUBMITTED→WORKING→COMPLETED/FAILED/CANCELED), opaque collaboration | Production |
| Guardrails | `guardrails.py` | Autonomous safety constraints: privilege escalation prevention, loop detection, destructive action gating, cost/token limits | Production |
| Persistent Memory | `persistent_memory.py` | SQLite-backed episodic/semantic/task memory with TTL, relevance scoring, capacity enforcement | Production |

### Platform Modules (Production)

| Module | File | Capability | Status |
|---|---|---|---|
| Gateway | `gateway.py` | WebSocket control plane, session management, heartbeat, TLS | Production |
| Channels | `channels/` | 8 messaging connectors (Discord, Slack, Telegram, WhatsApp, WebChat, IRC, Matrix, Signal) | Production |
| Channel Mesh | `channels/mesh.py` | Cross-channel identity linking, session continuity, per-channel policy overrides | Production |
| Skills | `skills.py` | Signed skill management with trust scores, permission scoping, evaluation harness (AG-02) | Production |
| Security | `security.py` | DM pairing, allowlists/denylists, secret detection, reputation tracking | Production |
| Browser | `browser.py` | Governed CDP browser automation with action audit trail | Production |
| Voice | `voice.py` | Multi-provider TTS/STT with failover chains | Production |
| Canvas | `canvas.py` | FSM-governed live canvas with snapshots, render timeout | Production |
| Sessions | `sessions.py` | A2A agent sessions with AG-05 role isolation | Production |
| Scheduler | `scheduler.py` | Cron/webhook/interval/one-shot automation with ELFE convergence | Production |
| MCP Server | `mcp_server.py` | Model Context Protocol server (JSON-RPC 2.0, stdio/SSE/WebSocket) | Production |

### Interface (Production)

| Module | File | Capability | Status |
|---|---|---|---|
| CLI | `cli.py` | Command center: run, onboard, doctor, gateway, skills, config, version, trace, replay, drift, providers, policy test, memory | Production |
| Web UI | `web_ui.py` | Operator console backend (React frontend in `web/`) | Production |

---

## Governance Guarantees (God File v∞.1 Alignment)

| Guarantee | Implementation | AG Reference |
|---|---|---|
| Fixed-time convergence | ELFE v∞.1 in `orchestrator.py` — drift → 0 within bounded T_max | — |
| Constraint closure | PolicyEngine + constraint projection C(x) in orchestrator | — |
| Proof-backed auditability | ProofVault WORM ledger + exportable receipts | — |
| Adaptive policy gating | PolicyEngine profiles (strict/balanced/exploratory) + contextual rules | — |
| Refusal as capability | Tested refusal pathways in policy engine + orchestrator | AG-07 |
| Agent mortality | Version-bound agents, no trans-repo identity | AG-03 |
| Evaluation before authority | Skills evaluation harness, no output without passing eval | AG-02 |
| Role isolation | Multi-agent orchestrator enforces single-role per agent | AG-05 |
| Repository-bound intelligence | Agent identity constrained to single versioned repo | AG-01 |
| Tool sovereignty | Explicit tool declaration, sandboxed execution, pre/post conditions | AG-04 |
| Non-proliferation | Fork hazard classification, clean-room mirror rule | AG-06 |

---

## Drift Model

Sovereign Claw decomposes drift into actionable components:

```
D_total(x) = D_tool + D_constraint + D_provider + D_policy

Where:
  D_tool       = drift from tool execution errors/penalties
  D_constraint = drift from constraint projection mismatches
  D_provider   = drift from provider failures/latency
  D_policy     = drift from policy violations or tightening
```

Each component is tracked independently and reported per execution step,
enabling targeted debugging and optimization.

---

## Provider Integration

| Provider | Type | Status |
|---|---|---|
| Anthropic | HTTP API (Claude) | Production |
| OpenAI | HTTP API (GPT) | Production |
| Gemini | HTTP API (Google) | Production |
| Perplexity | HTTP API | Production |
| Groq | HTTP API | Production |
| Mistral | HTTP API | Production |
| Ollama | Local HTTP | Production |
| Local | Custom endpoint | Production |
| Demo | In-process stub | **Dev-only** — not for production use |

---

## Memory Architecture

```
Memory Layer (in-memory)
├── Episodic Memory    — timestamped event records from execution traces
├── Semantic Memory    — extracted knowledge with embeddings + relevance scoring
└── Task Memory        — objective-specific context with TTL-based retention

Persistent Memory Layer (SQLite-backed, v3.1.0)
├── Episodic Memory    — capacity: 1000 entries
├── Semantic Memory    — capacity: 5000 entries
└── Task Memory        — capacity: 500 entries
│
├── TTL enforcement    — automatic expiry of stale memories
├── Relevance scoring  — weighted retrieval and decay
├── Capacity eviction  — oldest-first when limits exceeded
└── Drop-in compatible — same interface as in-memory MemoryStore

Governed by:
  - Retention policies (max entries, TTL expiry)
  - Relevance scoring for retrieval
  - ProofVault integration for audit trail
```

---

## Security Model

- **DM Pairing**: Channel-level identity verification
- **Allowlist/Denylist**: Configurable access control per channel
- **Secret Detection**: Automatic scanning of outbound messages
- **Reputation Tracking**: Byzantine reputation weighting per agent
- **Rate Limiting**: Global + per-channel rate limits
- **Audit Trail**: All security events logged to ProofVault

---

## Deployment

```
# Docker
docker compose up --build

# Exposes:
#   8765 — Gateway WebSocket
#   8766 — MCP server
#   9090 — Webhook receiver
```

---

## License

Apache-2.0
