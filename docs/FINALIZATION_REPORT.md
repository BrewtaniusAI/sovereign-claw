# Finalization Report

## Release
- Prior finalized release input: `v2.0.2`
- Upgraded release output: `v2.1.0`

## Validation
- `pytest -q` → `83 passed`
- `pytest --cov=sovereign_claw --cov-report=term-missing --cov-report=xml` configured in CI
- Lint/type-check stages added to CI and pre-commit

## Upgrades completed
- Added semantic-release configuration in `pyproject.toml`
- Added release automation in `.github/workflows/release.yml`
- Added `.pre-commit-config.yaml`
- Expanded `.github/workflows/ci.yml` with lint, format, type-check, coverage, and SBOM upload
- Added `scripts/generate_sbom.py`
- Added coverage configuration and release targets in `Makefile`
- Cleaned transient artifacts from the repository archive

## Remaining optional work
- Publish package to PyPI when repository secrets and trusted publishing are configured
- Add dependency update automation
- Add signed release attestations if the repository requires artifact provenance

## Rollback
- Restore the prior archive `sovereign-claw-v2.0.2-final.zip`
- Or revert the added files:
  - `.pre-commit-config.yaml`
  - `.github/workflows/release.yml`
  - `scripts/generate_sbom.py`
  - CI/README/Makefile/pyproject changes


## v2.2.0 high-impact upgrades
- Added provenance attestation workflow.
- Added policy engine and OPA starter policies.
- Added append-only event stream and replay support.
- Added hardened sandbox starter profile.
