#!/usr/bin/env bash
set -euo pipefail

IMAGE="${1:-python:3.12-slim}"
shift || true

docker run --rm       --read-only       --cap-drop=ALL       --security-opt=no-new-privileges:true       --pids-limit=256       --memory=512m       --cpus=1.0       --network=none       --tmpfs /tmp:rw,noexec,nosuid,size=64m       --security-opt seccomp="$(dirname "$0")/seccomp-minimal.json"       "${IMAGE}" "$@"
