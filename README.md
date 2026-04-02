# Sovereign Claw

[![CI](https://github.com/BrewtaniusAI/sovereign-claw/actions/workflows/ci.yml/badge.svg)](https://github.com/BrewtaniusAI/sovereign-claw/actions/workflows/ci.yml)
![Coverage](https://img.shields.io/badge/coverage-93%25-brightgreen)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)

**Deterministic, thermodynamically governed multi-agent framework with proof-vaulted execution traces, lane routing, drift control, and governance-oriented orchestration.**

> Part of the [CollectiveOS](https://github.com/BrewtaniusAI) ecosystem — the operator console and agent orchestration layer.

---

## Overview

Sovereign Claw provides a human-in-the-loop operator console for previewing and approving governed autonomous tasks. It implements the ELFE (Extended Lyapunov Fixed-time Equation) stability kernel, lane-based agent routing, and a full React-based operator interface.

---

## Status

- **Version:** v2.2.0
- **CI:** Passing (Python 3.10 / 3.11 / 3.12)
- **Coverage:** 93% (151 tests)
- **Pre-commit hooks:** Installed (ruff, mypy)

---

## Core Capabilities

| Capability | Description |
|-----------|-------------|
| **Deterministic Orchestration** | Constraint-driven execution with no uncontrolled drift |
| **Proof Vault** | Trace sealing + replay for full auditability |
| **Lane Routing** | Rabbit → Cypher → Giles specialized agent routing |
| **Drift Control** | ELFE-based bounded convergence and stability |
| **Policy Engine** | Local + optional OPA/Rego governance rules |
| **Event Logging** | Append-only JSONL stream logging |
| **Operator Console** | React + TypeScript frontend for human oversight |

### Lane Architecture

| Lane | Role | Function |
|------|------|----------|
| **Rabbit** | Draft | Fast initial generation and enumeration |
| **Cypher** | Critique | Security review and authority escalation blocking |
| **Giles** | Finalize | Arbitration, conflict resolution, final approval |

---

## Quickstart

```bash
# Install Python dependencies
python -m pip install -e .[dev]

# Run tests
pytest -q

# Common commands
make test
make package
make sbom
make sandbox-smoke
```

### Web Frontend

```bash
cd web
npm install
npm start
```

---

## Repository Structure

```
sovereign-claw/
├── src/sovereign_claw/           # Core Python logic
│   ├── cli.py                    # CLI interface
│   ├── graph_elve.py             # LangGraph workflow orchestration
│   ├── mythic_neuro_kernel.py    # ELFE skill transition engine
│   └── ...
├── web/                          # React operator console
│   ├── src/                      # TypeScript components
│   └── server.js                 # Express bridge to Python CLI
├── tests/                        # Full test suite (151 tests)
├── examples/                     # Runnable demos
├── scripts/                      # SBOM generator and tooling
├── docs/                         # Design artifacts and reports
├── .github/workflows/            # CI/CD pipeline
└── pyproject.toml                # Project configuration
```

---

## Key Concepts

- **Drift** — Numerical variance/error metric during objective execution
- **ELFE** — Extended Lyapunov Fixed-time Equation for skill convergence
- **Isomorphic Mastery** — Terminal state (1.0) in skill acquisition logic
- **Dongba Glyph** — Pictographic representation of system state
- **Wadjet Closure** — Logic gate for snapping near-complete states to mastery
- **Gardeners Protocol** — Persistence layer for skill ledgers ("scrolls")
- **Quipu Router** — Path selection logic for competency nodes
- **Task Manifold** — Set of constraints and forbidden actions for agents

---

## Guarantees

- Constraint-driven execution (no uncontrolled drift)
- Deterministic state transitions
- Safe failure via HALT semantics
- Auditability via Proof Vault receipts

---

## CollectiveOS Integration

- **ELFE Kernel** — Shared stability mathematics with Constraint Engine
- **Proof Vault** — Sealed execution traces for governance audit
- **Lane Routing** — Rabbit/Cypher/Giles roles map to CollectiveOS agent roles
- **Governance Pipeline** — QC → GATA → GATA PRIME enforcement

---

## License

Apache-2.0
