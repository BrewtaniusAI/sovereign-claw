from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.verify_community_distribution import APACHE_20_SHA256, verify_repository

ROOT = Path(__file__).resolve().parents[1]


def test_repository_community_license_contract() -> None:
    evidence = verify_repository(ROOT)
    assert evidence["license_sha256"] == APACHE_20_SHA256
    assert evidence["release_authorized"] is False
    assert hashlib.sha256((ROOT / "LICENSE").read_bytes()).hexdigest() == APACHE_20_SHA256


def test_distribution_manifest_is_non_authorizing_and_does_not_define_enterprise_terms() -> None:
    manifest = json.loads(
        (ROOT / "src/sovereign_claw/distribution_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["edition"] == "community"
    assert manifest["spdx_license_expression"] == "Apache-2.0"
    assert manifest["proprietary_additions_included"] is False
    assert manifest["enterprise_terms_defined"] is False
    assert manifest["release_authorized"] is False


def test_pyproject_uses_pep639_metadata_and_truthful_maturity() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'setuptools>=77.0.3' in pyproject
    assert 'license = "Apache-2.0"' in pyproject
    assert 'license-files = ["LICENSE", "NOTICE"]' in pyproject
    assert 'License :: OSI Approved :: Apache Software License' not in pyproject
    assert 'Development Status :: 5 - Production/Stable' not in pyproject
    assert 'Development Status :: 3 - Alpha' in pyproject
    assert 'sovereign_claw = ["distribution_manifest.json"]' in pyproject
