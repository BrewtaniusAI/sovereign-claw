#!/usr/bin/env python3
"""Verify exact bundled-asset inventory and provenance release readiness.

Inventory verification proves only that the manifest covers the exact tracked
bytes under its declared scope. Release verification additionally requires each
asset to have explicit source and license evidence. Neither mode authorizes a
release by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA = "sovereign-claw-bundled-asset-provenance-v1"
ALLOWED_STATUS = {"resolved", "unresolved"}


class ProvenanceError(RuntimeError):
    pass


def _git_blob_sha1(data: bytes) -> str:
    header = f"blob {len(data)}\0".encode("ascii")
    return hashlib.sha1(header + data).hexdigest()  # noqa: S324 - Git object identity


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvenanceError(f"invalid provenance manifest: {path}") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ProvenanceError("unsupported provenance manifest schema")
    if value.get("release_authorized") is not False:
        raise ProvenanceError("provenance manifest must never self-authorize release")
    scope = value.get("scope")
    if not isinstance(scope, str) or not scope or scope.startswith("/") or ".." in PurePosixPath(scope).parts:
        raise ProvenanceError("invalid provenance scope")
    assets = value.get("assets")
    if not isinstance(assets, list):
        raise ProvenanceError("assets must be a list")
    return value


def _tracked_files(root: Path, scope: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", scope],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ProvenanceError("unable to enumerate tracked asset files with git") from exc
    files = sorted(line for line in result.stdout.splitlines() if line)
    if not files:
        raise ProvenanceError(f"no tracked files found under provenance scope {scope!r}")
    return files


def verify_inventory(root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = _load_manifest(manifest_path)
    scope = str(manifest["scope"])
    tracked = _tracked_files(root, scope)

    records: dict[str, dict[str, Any]] = {}
    for raw in manifest["assets"]:
        if not isinstance(raw, dict):
            raise ProvenanceError("every asset record must be an object")
        path = raw.get("path")
        if not isinstance(path, str) or not path:
            raise ProvenanceError("every asset record requires a path")
        if path in records:
            raise ProvenanceError(f"duplicate asset record: {path}")
        if not path.startswith(scope.rstrip("/") + "/"):
            raise ProvenanceError(f"asset is outside declared scope: {path}")
        records[path] = raw

    recorded = sorted(records)
    if tracked != recorded:
        missing = sorted(set(tracked) - set(recorded))
        stale = sorted(set(recorded) - set(tracked))
        raise ProvenanceError(
            f"asset inventory mismatch; unrecorded={missing!r}; stale_records={stale!r}"
        )

    unresolved: list[str] = []
    evidence: list[dict[str, Any]] = []
    for path in tracked:
        record = records[path]
        status = record.get("status")
        if status not in ALLOWED_STATUS:
            raise ProvenanceError(f"{path}: invalid status {status!r}")
        file_path = root / path
        data = file_path.read_bytes()
        expected_size = record.get("size_bytes")
        expected_blob = record.get("git_blob_sha1")
        if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size < 0:
            raise ProvenanceError(f"{path}: invalid size_bytes")
        if expected_size != len(data):
            raise ProvenanceError(
                f"{path}: byte length drift: expected {expected_size}, got {len(data)}"
            )
        actual_blob = _git_blob_sha1(data)
        if expected_blob != actual_blob:
            raise ProvenanceError(
                f"{path}: Git blob drift: expected {expected_blob}, got {actual_blob}"
            )

        source = record.get("source")
        license_expression = record.get("license_expression")
        attribution = record.get("attribution")
        if status == "resolved":
            if not isinstance(source, str) or not source.strip():
                raise ProvenanceError(f"{path}: resolved asset requires explicit source evidence")
            if not isinstance(license_expression, str) or not license_expression.strip():
                raise ProvenanceError(f"{path}: resolved asset requires explicit license expression")
        else:
            if source is not None or license_expression is not None or attribution is not None:
                raise ProvenanceError(
                    f"{path}: unresolved asset must not carry speculative source/license/attribution"
                )
            unresolved.append(path)

        evidence.append(
            {
                "path": path,
                "git_blob_sha1": actual_blob,
                "size_bytes": len(data),
                "status": status,
            }
        )

    return {
        "schema": SCHEMA,
        "scope": scope,
        "assets": evidence,
        "unresolved": unresolved,
        "inventory_complete": True,
        "provenance_resolved": not unresolved,
        "release_authorized": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--manifest", type=Path, default=Path("THIRD_PARTY_PROVENANCE.json")
    )
    parser.add_argument("--release-gate", action="store_true")
    args = parser.parse_args()

    root = args.root.resolve()
    manifest_path = args.manifest
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    try:
        evidence = verify_inventory(root, manifest_path)
        if args.release_gate and evidence["unresolved"]:
            unresolved = ", ".join(evidence["unresolved"])
            raise ProvenanceError(f"release HOLD: unresolved bundled asset provenance: {unresolved}")
    except (OSError, ProvenanceError) as exc:
        print(f"BUNDLED_ASSET_PROVENANCE_FAILED: {exc}")
        return 2

    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
