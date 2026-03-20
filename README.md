# sovereign-claw

[![CI](https://github.com/BrewtaniusAI/sovereign-claw/actions/workflows/ci.yml/badge.svg)](https://github.com/BrewtaniusAI/sovereign-claw/actions/workflows/ci.yml)
![Coverage](https://img.shields.io/badge/coverage-93%25-brightgreen)
![License](https://img.shields.io/badge/license-Apache--2.0-blue)

Deterministic, thermodynamically governed multi-agent framework with proof-vaulted execution traces, lane routing, drift control, and governance-oriented orchestration.

---

## 🚀 Status

* ✅ CI: passing (Python 3.10 / 3.11 / 3.12)
* ✅ Coverage: **93%**
* ✅ Node 24–ready CI pipeline
* ✅ SBOM + release workflow configured
* ✅ Deterministic execution guarantees

---

## 📦 Release

This repository is finalized as **v2.2.0**.

### Highlights

* Cleaned repository artifacts (`__pycache__`, `.pytest_cache`, compiled bytecode)
* Apache-2.0 `LICENSE`
* Reproducible `Makefile`
* Full CI pipeline (lint, type-check, test, coverage, SBOM)
* Semantic release automation
* Pre-commit hooks
* SBOM generator (`scripts/generate_sbom.py`)
* Finalization report (`docs/FINALIZATION_REPORT.md`)

---

## 🧠 Core Capabilities

* Deterministic orchestration primitives
* Proof Vault trace sealing + replay
* Lane-based agent routing (Rabbit → Cypher → Giles)
* Drift-aware execution + bounded convergence
* ELFE graph loop execution model
* Policy engine (local + optional OPA/Rego)
* Tiered backend routing (local + cloud)
* Event stream logging (append-only JSONL)

---

## ⚡ Quick Start

```bash
python -m pip install -e .[dev]
pytest -q
```

---

## 🛠 Common Commands

```bash
make test
make package
make sbom
make sandbox-smoke
```

---

## 🧪 Smoke Tests

```bash
python examples/04_kitaev_penalty_tiers.py
python examples/05_full_swarm_demo.py
```

---

## 📁 Project Structure

* `src/sovereign_claw/` — core system
* `tests/` — full test suite
* `examples/` — runnable demos
* `scripts/` — tooling (SBOM, etc.)
* `docs/` — reports and design artifacts

---

## 🧾 Notes

* Uses `src/` layout
* Tests run without install (via `tests/conftest.py`)
* No external services required for unit tests
* All backends are mock-tested for deterministic CI

---

## 🔒 Guarantees

* Constraint-driven execution (no uncontrolled drift)
* Deterministic state transitions
* Safe failure via HALT semantics
* Auditability via Proof Vault receipts

---

## 📜 License

Apache-2.0
