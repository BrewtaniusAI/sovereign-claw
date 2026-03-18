# sovereign-claw

[![CI](https://github.com/brewtanius/sovereign-claw/actions/workflows/ci.yml/badge.svg)](https://github.com/brewtanius/sovereign-claw/actions/workflows/ci.yml)
![Coverage target](https://img.shields.io/badge/coverage-target%2085%25-brightgreen)

Deterministic, thermodynamically governed agent framework with proof-vaulted execution traces, lane routing, drift control, and governance-oriented orchestration.

## Release status

This package has been cleaned and upgraded as **v2.2.0**.

Notable finalization upgrades:
- repository artifacts cleaned (`__pycache__`, `.pytest_cache`, compiled bytecode removed)
- package version aligned to `2.1.0`
- Apache-2.0 `LICENSE` file added
- reproducible local `Makefile` added
- GitHub Actions CI workflow expanded for lint, type checks, coverage, and SBOM
- semantic-release automation added for version tags and GitHub releases
- pre-commit hooks added for local quality gates
- SBOM generator added under `scripts/generate_sbom.py`
- finalization report added under `docs/FINALIZATION_REPORT.md`

## What is included

- Deterministic orchestration primitives
- Proof Vault trace sealing
- Lane routing and risk-aware halting
- ELFE graph loop support
- Giles and Ollama backend adapters
- Mythic / Weavers / Gardener protocol layers
- Examples and tests

## Quick start

```bash
python -m pip install -e .[dev]
pytest -q
```

## Common commands

```bash
make test
make package
```

## Smoke test

```bash
python examples/04_kitaev_penalty_tiers.py
python examples/05_full_swarm_demo.py
```

## Notes

This repo uses a `src/` layout. Tests are configured to work directly from the repository checkout via `tests/conftest.py`, so `pytest` works without a prior editable install.


## High-impact upgrades in v2.2.0

- Release provenance workflow via GitHub artifact attestations
- Policy engine with deterministic local rules and optional OPA/Rego enforcement
- Append-only JSONL event stream for Proof Vault replay
- Hardened container sandbox profile for isolated execution smoke tests

## Extra commands

```bash
make sbom
make sandbox-smoke
```
