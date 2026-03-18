PYTHON ?= python

.PHONY: install test lint fmt typecheck coverage package clean precommit sbom release-check

install:
	$(PYTHON) -m pip install -e .[dev]

test:
	pytest -q

lint:
	ruff check src tests examples

fmt:
	ruff format src tests examples

typecheck:
	mypy src

coverage:
	pytest --cov=sovereign_claw --cov-report=term-missing --cov-report=xml

package:
	$(PYTHON) -m build

precommit:
	pre-commit run --all-files

sbom:
	$(PYTHON) scripts/generate_sbom.py

release-check:
	python -m build
	pytest --cov=sovereign_claw --cov-report=term-missing --cov-report=xml

clean:
	rm -rf build dist .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml sbom *.egg-info src/*.egg-info src/sovereign_claw.egg-info

attest-verify:
	bash scripts/verify_attestation.sh dist/*

sandbox-smoke:
	bash sandbox/run_hardened_container.sh python:3.12-slim python -V
