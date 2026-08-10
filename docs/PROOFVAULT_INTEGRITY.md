# ProofVault Write-Time Integrity Contract

Status: implementation contract for issue #15. This document describes the authority boundary that the code in this branch must enforce; it is not a claim that every item below is implemented until the corresponding tests are green.

## Authority model

`ProofVault` is the authoritative local evidence store. Local SQLite is **tamper-evident**, not physically immutable WORM storage. `EventStream` is a derived/operator mirror and must never be treated as a second authority.

Authoritative evidence is appended to a versioned `evidence_records` log. Each record carries a globally monotonic sequence, evidence type, trace/correlation identity, canonical payload digest/material, `prev_hash`, `record_hash`, timestamp representation, schema/hash version, and optional authority metadata required by the caller.

The record hash is computed from canonical record material and the stored previous hash. Canonical JSON is UTF-8, sorted-key, compact-separator JSON with non-finite numbers rejected. Unsupported or cyclic values fail closed rather than being stringified.

## Atomic append

Every authoritative append uses an explicit write transaction (`BEGIN IMMEDIATE` for SQLite local mode):

1. Read and verify the durable chain tip and previous record.
2. Validate domain constraints, including per-trace step identity/order where applicable.
3. Canonicalize the record and compute `record_hash` from `prev_hash || canonical_record`.
4. Insert the immutable evidence row.
5. Advance durable tip/checkpoint metadata.
6. Commit once.

Any mismatch or write failure rolls the transaction back. Concurrent writers must serialize to one total order; two children of the same tip are not valid.

## Database immutability boundary

SQLite `BEFORE UPDATE` and `BEFORE DELETE` triggers protect immutable evidence rows. Mutable operational data such as reputation/derived indexes stays outside the immutable evidence table. Maintenance/migration cannot silently disable the guard in normal runtime operation.

Connections enable foreign keys and a bounded busy timeout. WAL is permitted for concurrency but is not itself considered serialization of the chain-tip race.

## Legacy migration

Rows created before this write-time chain existed have no historical write-time proof. Migration MUST NOT retroactively certify them.

Existing unchained records are represented as `LEGACY_UNVERIFIED` provenance (or equivalent). A one-time migration/import checkpoint may attest to a snapshot **from migration time forward**, but it cannot turn pre-migration history into verified evidence. Receipts containing legacy segments expose that provenance and remain unverified unless an independently trusted earlier checkpoint exists.

New post-migration evidence begins at a defined genesis/checkpoint and is fully chained.

## Verification

`ProofVault.verify_chain()` (or equivalent) walks stored authority records and validates at minimum:

- genesis/checkpoint semantics;
- contiguous global sequence;
- stored `prev_hash` linkage;
- recomputed canonical `record_hash`;
- durable tip/checkpoint consistency;
- record type and trace/correlation constraints;
- duplicate/colliding step identities;
- legacy provenance boundaries.

Verification fails closed and returns a bounded first-failure classification/sequence. It never auto-heals history.

## Receipts and replay

`ReceiptBuilder` consumes stored evidence hashes. It does not invent a new hash chain from mutable projection rows. `verified=True` is only possible after a real stored-chain verification pass that binds the receipt's declared root/tip/checkpoint to the verified ledger state.

Replay of authoritative history requires successful evidence verification first. A JSONL mirror that is truncated, edited, duplicated, or reordered cannot be presented as verified replay.

## EventStream

After ProofVault commit, EventStream may receive a derived record carrying at least source sequence and source record hash. Mirror write failure does not rewrite or fabricate authority. Mirror lag/failure must be observable and the mirror must be resynchronizable from the authoritative chain.

## Required adversarial coverage

The implementation is not complete until tests cover:

- historical payload/hash mutation detection;
- immutable UPDATE/DELETE trigger enforcement;
- missing/deleted/reordered sequence detection;
- forged `prev_hash` or durable tip detection;
- duplicate per-trace step identity rejection;
- two-writer append serialization;
- rollback when append/tip advancement fails;
- restart verification with stable root/tip;
- deterministic canonical hashing;
- rejection of NaN/Infinity/unserializable/cyclic payloads;
- legacy database migration remaining explicitly unverified;
- receipt construction never asserting verification without a successful ledger verification pass.

External signing, remote object-lock/WORM replication, and public checkpoint notarization are deliberately deferred to a later assurance layer.