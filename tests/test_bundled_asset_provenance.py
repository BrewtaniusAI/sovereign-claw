from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.verify_bundled_asset_provenance import (
    ProvenanceError,
    _git_blob_sha1,
    verify_inventory,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "THIRD_PARTY_PROVENANCE.json"


def test_inventory_covers_exact_tracked_public_assets() -> None:
    evidence = verify_inventory(ROOT, MANIFEST)
    assert evidence["inventory_complete"] is True
    assert evidence["provenance_resolved"] is False
    assert evidence["release_authorized"] is False
    assert evidence["unresolved"] == [
        "web/public/favicon.svg",
        "web/public/giles.png",
        "web/public/icons.svg",
    ]


def test_manifest_blob_identities_match_exact_repository_bytes() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for record in manifest["assets"]:
        data = (ROOT / record["path"]).read_bytes()
        assert len(data) == record["size_bytes"]
        assert _git_blob_sha1(data) == record["git_blob_sha1"]


def test_release_gate_fails_while_any_asset_is_unresolved() -> None:
    result = subprocess.run(
        [
            "python",
            "scripts/verify_bundled_asset_provenance.py",
            "--release-gate",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "release HOLD" in result.stdout
    assert "favicon.svg" in result.stdout
    assert "giles.png" in result.stdout
    assert "icons.svg" in result.stdout


def test_unresolved_record_cannot_smuggle_speculative_license(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    modified = copy.deepcopy(manifest)
    modified["assets"][0]["license_expression"] = "Apache-2.0"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(modified), encoding="utf-8")

    with pytest.raises(ProvenanceError, match="must not carry speculative"):
        verify_inventory(ROOT, path)


def test_resolved_record_requires_source_and_license(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    modified = copy.deepcopy(manifest)
    modified["assets"][0]["status"] = "resolved"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(modified), encoding="utf-8")

    with pytest.raises(ProvenanceError, match="requires explicit source evidence"):
        verify_inventory(ROOT, path)


def test_git_blob_helper_matches_git_hash_object() -> None:
    path = ROOT / "web/public/icons.svg"
    expected = subprocess.run(
        ["git", "hash-object", str(path.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert _git_blob_sha1(path.read_bytes()) == expected
