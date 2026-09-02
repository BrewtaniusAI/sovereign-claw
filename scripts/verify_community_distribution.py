#!/usr/bin/env python3
"""Verify Community-edition license metadata in repository and built artifacts.

This verifier is evidence-only. A passing result does not authorize release and
it does not define proprietary or Enterprise license terms.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import zipfile
from email.parser import Parser
from pathlib import Path

# Canonical Apache-2.0 content hash after normalizing line endings and removing
# only outer blank lines. Internal wording, spacing, and line structure remain
# part of the verified content contract.
APACHE_20_NORMALIZED_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"
EXPECTED_LICENSE_EXPRESSION = "Apache-2.0"
MANIFEST_RELATIVE = Path("src/sovereign_claw/distribution_manifest.json")


class DistributionVerificationError(RuntimeError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalized_text_sha256(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DistributionVerificationError("LICENSE must be UTF-8 text") from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n") + "\n"
    return _sha256(normalized.encode("utf-8"))


def _read_manifest_bytes(data: bytes, source: str) -> dict[str, object]:
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DistributionVerificationError(f"invalid distribution manifest: {source}") from exc
    if not isinstance(manifest, dict):
        raise DistributionVerificationError(f"distribution manifest must be an object: {source}")
    expected = {
        "schema": "sovereign-claw-distribution-v1",
        "edition": "community",
        "spdx_license_expression": EXPECTED_LICENSE_EXPRESSION,
        "license_files": ["LICENSE", "NOTICE"],
        "proprietary_additions_included": False,
        "enterprise_terms_defined": False,
        "release_authorized": False,
    }
    if manifest != expected:
        raise DistributionVerificationError(
            f"distribution manifest does not match fail-closed Community contract: {source}"
        )
    return manifest


def verify_repository(root: Path) -> dict[str, object]:
    license_path = root / "LICENSE"
    notice_path = root / "NOTICE"
    manifest_path = root / MANIFEST_RELATIVE
    for path in (license_path, notice_path, manifest_path):
        if not path.is_file():
            raise DistributionVerificationError(f"required Community file is missing: {path}")

    license_bytes = license_path.read_bytes()
    if _normalized_text_sha256(license_bytes) != APACHE_20_NORMALIZED_SHA256:
        raise DistributionVerificationError("LICENSE does not match canonical Apache-2.0 content")
    notice_bytes = notice_path.read_bytes()
    if not notice_bytes.strip():
        raise DistributionVerificationError("NOTICE must not be empty")
    manifest = _read_manifest_bytes(manifest_path.read_bytes(), str(manifest_path))
    return {
        "license_sha256": _sha256(license_bytes),
        "license_normalized_sha256": APACHE_20_NORMALIZED_SHA256,
        "notice_sha256": _sha256(notice_bytes),
        "manifest": manifest,
        "release_authorized": False,
    }


def _metadata_contract(metadata: str, source: str) -> None:
    message = Parser().parsestr(metadata)
    if message.get("License-Expression") != EXPECTED_LICENSE_EXPRESSION:
        raise DistributionVerificationError(
            f"{source}: License-Expression must be {EXPECTED_LICENSE_EXPRESSION}"
        )
    license_files = set(message.get_all("License-File", []))
    if not {"LICENSE", "NOTICE"}.issubset(license_files):
        raise DistributionVerificationError(
            f"{source}: License-File metadata must include LICENSE and NOTICE; got {sorted(license_files)}"
        )


def verify_wheel(path: Path, *, expected_license_sha256: str) -> dict[str, object]:
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise DistributionVerificationError(f"invalid wheel: {path}") from exc
    with archive:
        names = archive.namelist()
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        license_names = [name for name in names if name.endswith(".dist-info/licenses/LICENSE")]
        notice_names = [name for name in names if name.endswith(".dist-info/licenses/NOTICE")]
        manifest_names = [name for name in names if name == "sovereign_claw/distribution_manifest.json"]
        if len(metadata_names) != 1:
            raise DistributionVerificationError(f"wheel must contain exactly one METADATA: {path}")
        if len(license_names) != 1 or len(notice_names) != 1:
            raise DistributionVerificationError(
                f"wheel must ship exactly one LICENSE and NOTICE under dist-info/licenses: {path}"
            )
        if len(manifest_names) != 1:
            raise DistributionVerificationError(
                f"wheel must ship the Community distribution manifest: {path}"
            )
        metadata = archive.read(metadata_names[0]).decode("utf-8")
        _metadata_contract(metadata, str(path))
        wheel_license = archive.read(license_names[0])
        if _sha256(wheel_license) != expected_license_sha256:
            raise DistributionVerificationError(
                f"wheel LICENSE bytes differ from repository LICENSE: {path}"
            )
        if _normalized_text_sha256(wheel_license) != APACHE_20_NORMALIZED_SHA256:
            raise DistributionVerificationError(f"wheel LICENSE content is not canonical: {path}")
        notice_bytes = archive.read(notice_names[0])
        if not notice_bytes.strip():
            raise DistributionVerificationError(f"wheel NOTICE is empty: {path}")
        _read_manifest_bytes(archive.read(manifest_names[0]), str(path))
    return {"artifact": path.name, "type": "wheel", "gate": "pass"}


def verify_sdist(path: Path, *, expected_license_sha256: str) -> dict[str, object]:
    try:
        archive = tarfile.open(path, "r:*")
    except (OSError, tarfile.TarError) as exc:
        raise DistributionVerificationError(f"invalid sdist: {path}") from exc
    with archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        names = [member.name for member in members]
        package_info = [name for name in names if name.count("/") == 1 and name.endswith("/PKG-INFO")]
        license_names = [name for name in names if name.count("/") == 1 and name.endswith("/LICENSE")]
        notice_names = [name for name in names if name.count("/") == 1 and name.endswith("/NOTICE")]
        manifest_names = [
            name for name in names if name.endswith("/src/sovereign_claw/distribution_manifest.json")
        ]
        if len(package_info) != 1:
            raise DistributionVerificationError(f"sdist must contain exactly one root PKG-INFO: {path}")
        if len(license_names) != 1 or len(notice_names) != 1:
            raise DistributionVerificationError(f"sdist must ship root LICENSE and NOTICE: {path}")
        if len(manifest_names) != 1:
            raise DistributionVerificationError(
                f"sdist must ship the Community distribution manifest: {path}"
            )

        def read_member(name: str) -> bytes:
            member = archive.getmember(name)
            stream = archive.extractfile(member)
            if stream is None:
                raise DistributionVerificationError(f"cannot read sdist member: {name}")
            return stream.read()

        _metadata_contract(read_member(package_info[0]).decode("utf-8"), str(path))
        sdist_license = read_member(license_names[0])
        if _sha256(sdist_license) != expected_license_sha256:
            raise DistributionVerificationError(
                f"sdist LICENSE bytes differ from repository LICENSE: {path}"
            )
        if _normalized_text_sha256(sdist_license) != APACHE_20_NORMALIZED_SHA256:
            raise DistributionVerificationError(f"sdist LICENSE content is not canonical: {path}")
        notice_bytes = read_member(notice_names[0])
        if not notice_bytes.strip():
            raise DistributionVerificationError(f"sdist NOTICE is empty: {path}")
        _read_manifest_bytes(read_member(manifest_names[0]), str(path))
    return {"artifact": path.name, "type": "sdist", "gate": "pass"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    args = parser.parse_args()
    try:
        repository_evidence = verify_repository(args.root)
        expected_license_sha256 = str(repository_evidence["license_sha256"])
        artifact_evidence: list[dict[str, object]] = []
        for artifact in args.artifact:
            if artifact.suffix == ".whl":
                artifact_evidence.append(
                    verify_wheel(artifact, expected_license_sha256=expected_license_sha256)
                )
            elif artifact.name.endswith(".tar.gz"):
                artifact_evidence.append(
                    verify_sdist(artifact, expected_license_sha256=expected_license_sha256)
                )
            else:
                raise DistributionVerificationError(f"unsupported distribution artifact: {artifact}")
        evidence: dict[str, object] = {
            "repository": repository_evidence,
            "artifacts": artifact_evidence,
        }
    except DistributionVerificationError as exc:
        print(f"COMMUNITY_DISTRIBUTION_FAILED: {exc}")
        return 2
    print(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
