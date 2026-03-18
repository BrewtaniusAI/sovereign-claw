#!/usr/bin/env bash
set -euo pipefail

ARTIFACT="${1:?artifact path required}"
cosign verify-blob-attestation       --new-bundle-format       --bundle "${ARTIFACT}.bundle"       "${ARTIFACT}"
