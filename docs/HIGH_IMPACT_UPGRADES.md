# High-impact upgrades

This release adds the next governance layer beyond CI and SBOM hygiene.

## 1. Build provenance and attestations
- `.github/workflows/provenance.yml` builds distribution artifacts and emits GitHub-native build provenance attestations.
- `scripts/verify_attestation.sh` is a verifier entrypoint for Sigstore/cosign-based artifact verification.

## 2. Policy engine
- `src/sovereign_claw/policy_engine.py` adds deterministic local checks.
- `policies/execution.rego` provides a starter OPA/Rego policy pack.
- `Orchestrator` now evaluates each tool decision before execution and halts with a policy reason when denied.

## 3. Event sourcing and replay
- `src/sovereign_claw/event_stream.py` writes append-only JSONL events.
- `ProofVault` can mirror trace creation and step append events into the stream.
- Replay state can be reconstructed without querying SQLite directly.

## 4. Sandboxing starter
- `sandbox/run_hardened_container.sh` runs an isolated no-network container with dropped capabilities.
- `sandbox/seccomp-minimal.json` provides a minimal syscall profile as a hardening baseline.

## Suggested next step
For full production isolation, replace the Docker starter with Firecracker or gVisor-backed execution and attach attestation verification to release promotion gates.
