# Batch 01 — measured closure authority defect ledger

Status: **LOCAL_CANDIDATE**

| ID | Authority defect | Resolution | Verification |
|---|---|---|---|
| A | Strings accepted as persistence proof | ProofVault alone mints `VerifiedEvidenceBindingV1` after full-chain and exact-record verification | Membership, trace, type, payload, provenance and tamper tests |
| B | Caller-provided closure hashes authorize lanes | `LaneRouter.authorize_closure` requires the exact vault binding and closure decision | Forged binding/hash tests |
| C | Missing provider uncertainty becomes measured zero | `None` is represented as `UNMEASURED`; required unknowns block closure | Unknown-provider test |
| D | `MEASURED` label lacks evidence | Every required component is bound to a verified `component.measurement.v1` record and assessment | Missing/fabricated/stale/wrong-trace tests |
| E | Legacy scalar lane shortcut | `advance` is explicitly compatibility-only and never enters `AUTHORITATIVE` | Zero-drift legacy test |
| F | Legacy thermodynamic scalar closure | Scalar zero reports `LEGACY_MODEL_ZERO`, distinct from verified closure | Zero-drift legacy test |
| G | Caller certificate registry is trusted | Fixed-time claims require a frozen server-owned digest allow-list with complete identity/domain bindings | Trust/signature/staleness/mismatch tests |
| H | Frozen registry retains mutable evaluator | Registry snapshots callable implementation digest and revalidates immediately before use | Post-freeze mutation test |
| I | Source-mutating cleanup workflow | Workflow absent; cleanup is local-only | Repository check |

Remote reconciliation is currently blocked in this environment: HTTPS fetch of
the repository fails with `CONNECT tunnel failed, response 403`. No remote push
or merge is attempted from this local candidate.
