# ToolSpecV1 Authority Contract

Status: implementation contract for issue #40. This branch is intentionally based on the current integrated `main` and must be updated onto the exact merged #46/#15 head before final promotion.

## Purpose

Replace approval authority derived from Python implementation details (`inspect.signature()` and raw callables) with a deterministic, versioned tool contract. The same immutable contract must govern preview, approval, execution dispatch, postcondition validation, and evidence.

This layer is the prerequisite for #16's production worker boundary. #16 may execute only a server-owned `worker_handler_id` selected from the exact approved registry snapshot; arbitrary callables, module paths, or client/model-selected handlers never cross IPC.

## Root invariants

1. **One tool identity, one immutable authority contract.** A production tool is identified by a stable `tool_id`, recommended form `namespace/name@semantic-version`, plus the canonical hash of its `ToolSpecV1`.
2. **Approval binds the reviewed contract, not Python syntax.** `inspect.signature()` may help generate development scaffolding but cannot contribute authority in production.
3. **Registry state is atomic.** A registry entry binds exactly `(ToolSpecV1, tool_contract_hash, worker_handler_id)`. Handler substitution under the same contract is forbidden.
4. **Validation occurs before authority and before dispatch.** Input schema validation happens before preview can be approvable and is repeated immediately before execution. Output/postcondition validation happens before success/closure can be asserted.
5. **Contract drift means zero actuation.** If the ToolSpec, handler, capability set, policy/config identity, or canonical arguments differ from the approved snapshot, execution fails with a stable mismatch class and requires a new preview/approval.
6. **Production has no raw-callable authority path.** Legacy callable registration, if retained, is explicit development-only behavior and cannot mint a production ExecutionIntent.
7. **Filesystem authority is capability-based.** Built-ins do not receive unrestricted host path authority. A server-owned root/capability ID plus bounded relative path is the authority surface.
8. **Evidence is bounded and privacy-safe.** Store hashes, sizes, classifications, contract IDs, policy/intent identities, postcondition results, and sanitized failure classes; do not store private chain-of-thought or unnecessary secret/content bodies.

## ToolSpecV1 canonical fields

`ToolSpecV1` is immutable/frozen and has a schema version. Canonical bytes use deterministic finite JSON: UTF-8, sorted keys, compact separators, no NaN/Infinity, no implementation-specific repr strings.

Minimum authority fields:

- `schema_version`
- `tool_id`
- `tool_version`
- `description_hash` or non-authoritative display description separated from authority material
- `input_schema`
- `output_schema`
- `capabilities`
- `risk_class`
- `required_principal_scopes`
- `isolation_profile`
- `worker_handler_id`
- `worker_build_identity` / image identity where applicable
- `default_deadline_ms`
- `max_deadline_ms`
- `max_input_bytes`
- `max_output_bytes`
- `reversibility`
- `idempotency`
- `postcondition_validator_id`
- `postcondition_validator_version`
- `evidence_policy`
- `redaction_policy`

Schemas must be strict. Object schemas default to `additionalProperties: false`; unknown keys do not silently pass through.

`tool_contract_hash = SHA256(canonical_json(authority_fields))`.

## Registry

Introduce a `ToolRegistry` whose authoritative entry is a frozen record similar to:

```text
ToolRegistryEntry(
  spec: ToolSpecV1,
  tool_contract_hash: sha256,
  worker_handler_id: stable server-owned ID,
  trusted_execution_class: optional explicit in-process class,
)
```

Registration must reject:

- duplicate `tool_id` with a different contract without explicit version change;
- a supplied hash that differs from recomputed canonical hash;
- unknown/invalid isolation profiles;
- invalid schemas or authority fields;
- handler substitution under an existing immutable contract;
- production raw callables without an approved spec.

A registry snapshot used during preview must be identifiable so execution can prove it is dispatching the same contract.

## Preview and action digest

Replace `tool_schema = str(inspect.signature(tool_fn))` in the current action canonicalization.

Production preview:

1. Resolve model-proposed tool name to a server-owned registry entry.
2. Validate and canonicalize bounded arguments against `ToolSpecV1.input_schema` without importing/executing the handler.
3. Evaluate policy/risk/capability constraints.
4. Build the action authority material from the exact ToolSpec hash and canonical arguments.

Recommended authority form:

```text
action_digest = SHA256(canonical_json({
  action_version,
  tool_id,
  tool_contract_hash,
  canonical_args,
  policy_bundle_hash_or_profile_identity,
  config_identity_hash,
  principal_or_execution_context_identity,
}))
```

Preview and pre-dispatch must invoke the same canonical function. A ToolSpec or registry change after preview produces `TOOL_CONTRACT_CHANGED` / `APPROVED_ACTION_MISMATCH` with zero handler calls.

## Dispatch handoff to #16

#16 receives only bounded structured data:

- exact approved `tool_id`
- exact `tool_contract_hash`
- server-owned `worker_handler_id`
- validated canonical args
- ExecutionIntent/action digest identity
- requested capability manifest
- exact isolation profile/build identity
- deadline and I/O caps
- bounded execution context/evidence correlation IDs

The worker resolves handlers from a server-controlled allowlist. Never pickle or transmit an arbitrary Python callable or a model/client-selected import path.

## Output and postconditions

A handler returning normally is not sufficient for success.

Before success/closure:

1. Bound serialized result size.
2. Validate result against strict `output_schema`.
3. Run the declared postcondition validator.
4. Verify side-effect evidence required by the ToolSpec.
5. Persist bounded evidence through the authoritative ProofVault interface from #15.

Failures use stable classifications and zero private traceback/model-context leakage.

## Filesystem capability migration

Current built-ins accept arbitrary host `path: str`; production authority must replace that with server-created capability roots/handles.

### Common path rules

- request carries `root_id`/capability handle plus relative path;
- reject absolute paths, `..` traversal, NULs, device/special files, root replacement, and unauthorized symlink escapes;
- prefer descriptor/root-relative operations and regular-file/directory checks;
- effective root/capability identity is part of authority material;
- enforce explicit byte/entry limits.

### `read_text_file`

- regular file only;
- explicit maximum bytes;
- explicit UTF-8/error policy;
- secret-class roots require an explicit capability;
- evidence stores digest/size/classification by default, not file contents.

### `list_directory`

- approved directory root only;
- bounded entry count and serialized bytes;
- stable sorted result;
- no implicit symlink traversal outside the root.

### `write_json_file`

- finite canonical JSON;
- explicit maximum bytes;
- overwrite denied by default;
- atomic temp-write + flush/fsync + replace inside the approved root;
- verify resulting digest/postcondition;
- overwrite, if permitted, requires explicit authority in ToolSpec/ExecutionIntent.

## Compatibility

A development compatibility adapter may infer a draft schema from a Python callable, but it must be visibly non-production and must not create an approvable production action unless a reviewed immutable ToolSpec has been registered.

Existing public tool names can remain aliases, but authority resolves to stable ToolSpec IDs.

## Required tests

At minimum:

- deterministic ToolSpec canonicalization/hash across restart;
- same Python signature + different ToolSpec/version/handler => different contract/action digest;
- spec mutation after preview => zero tool calls;
- handler substitution under same tool name/hash rejected;
- missing/unknown/extra kwargs rejected before approval;
- non-finite/oversize/deep inputs rejected before approval;
- output schema/size/postcondition failure cannot be reported as success;
- production raw callable cannot mint ExecutionIntent authority;
- unknown handler ID rejected before worker dispatch;
- scoped filesystem read/list/write traversal, symlink, absolute-path, special-file, overwrite, and byte-cap adversarial cases;
- same ToolSpec + same canonical args + same policy/config identity produces stable action digest across process restart;
- any authority field change changes `tool_contract_hash` and therefore action digest.

## Integration sequence

1. Merge #15 / PR #46 and update this branch onto that exact `main` head.
2. Implement `ToolSpecV1`, `ToolRegistry`, strict validation, action-digest migration, and scoped built-in capabilities in #40.
3. Make #16 consume only the approved registry entry/handler ID and append execution evidence through the #15 ProofVault authority path.
4. Remove or permanently gate any production raw-callable dispatch path.

## Exit criterion

Production preview, approval, and dispatch all bind the same immutable `tool_contract_hash`; handler/spec changes cannot reuse prior authority; unknown/unregistered raw callables cannot actuate; filesystem authority is scope-bounded; result/postcondition evidence is verified before success; and the full required CI/CodeQL/GHAS gates pass on the exact merge head.
