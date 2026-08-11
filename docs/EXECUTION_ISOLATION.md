# Bounded Execution Isolation Contract

Status: implementation contract for issue #16. This branch is intentionally contract-only until PR #47 / issue #40 merges. Before implementation is promoted, this branch must be updated onto the exact merged #47 `main` head so execution consumes the authoritative `ToolSpecV1` / `ToolRegistryEntry` contract rather than raw Python callables.

## Purpose

Replace the current advisory in-process `KitaevZeroMode.execute_safely()` actuation path with a real, bounded worker boundary. Governance, approval, and drift penalties remain separate from physical execution containment.

The worker boundary must consume only server-owned handler identities and validated structured data already authorized by #40. Model/client output never selects a Python import path, module, callable, executable, shell command, container image, or sandbox implementation.

## Root invariants

1. **Governance is not isolation.** Policy, ToolSpec validation, approval, drift control, and Kitaev penalties determine whether work may be attempted; they are not a sandbox.
2. **No arbitrary callable crosses the boundary.** Production dispatch carries a server-owned `worker_handler_id` from the exact approved `ToolRegistryEntry`.
3. **Approval and execution are isomorphic.** The worker request binds the exact `tool_id`, `tool_contract_hash`, action/ExecutionIntent digest, registry snapshot identity, handler ID, worker build identity, isolation profile, canonical arguments, capabilities, deadlines, and resource limits approved before dispatch.
4. **Context drift means zero actuation.** Any mismatch between approved authority and the dispatch-time registry/spec/handler/build/capability context fails before worker creation.
5. **Limits are enforced outside the tool.** Wall-clock deadline, process-tree cancellation, output caps, and any claimed CPU/memory/process/file/network restrictions are controlled by the parent/sandbox boundary, not trusted to tool code.
6. **No silent downgrade.** If the requested isolation profile or capability restriction cannot be enforced on the current host, dispatch fails closed. It never falls back to a weaker profile without a new approval binding that exact weaker profile.
7. **A normal return is not success.** Output bounds/schema, declared postconditions, required side-effect evidence, and authority evidence must validate before success/closure is recorded.
8. **Evidence is bounded and sanitized.** ProofVault receives hashes, sizes, stable classifications, timing/resource summaries, authority identities, and redacted diagnostics; it does not store private chain-of-thought, unrestricted stdout/stderr, file bodies, tokens, or secrets by default.
9. **Claims match enforcement.** `subprocess_bounded` may be called process/resource containment only. It must not be described as filesystem/network sandboxing unless an OS-backed profile actually enforces those restrictions.

## WorkerRequestV1

Define an immutable/versioned request serialized as strict canonical UTF-8 JSON with `additionalProperties=false` semantics and explicit byte/depth limits.

Minimum authority fields:

- `schema_version`
- `request_id`
- `trace_id`
- `correlation_id`
- `tool_id`
- `tool_contract_hash`
- `registry_snapshot_hash`
- `worker_handler_id`
- `worker_build_identity`
- `action_digest` / ExecutionIntent identity
- `policy_identity`
- `principal_identity`
- `isolation_profile`
- validated canonical `args`
- declared capability manifest
- `deadline_ms`
- `cpu_budget_ms` where enforceable
- `memory_bytes` where enforceable
- `max_processes`
- `max_output_bytes`
- filesystem capability handles/root IDs, never arbitrary host roots supplied by the model/client
- network capability policy/allowlist identity if a network-capable profile is used
- postcondition validator identity/version
- evidence/redaction policy identity

The parent recomputes and verifies the request authority immediately before launch. The child does not decide or broaden any field.

## WorkerResponseV1

The worker returns one bounded structured result envelope. Large/untrusted outputs do not flow unbounded through pipes.

Minimum fields:

- `schema_version`
- `request_id`
- stable status class: `SUCCEEDED`, `TOOL_ERROR`, `TIMEOUT`, `CANCELLED`, `WORKER_CRASH`, `RESOURCE_LIMIT`, `CAPABILITY_DENIED`, `OUTPUT_LIMIT`, `PROTOCOL_ERROR`, `POSTCONDITION_FAILED`
- sanitized result or result reference only if within the ToolSpec output policy
- `result_sha256`
- `result_size_bytes`
- bounded/sanitized diagnostic class and message
- runtime duration
- observed resource summary when trustworthy
- side-effect evidence digests required by ToolSpec

Malformed, oversized, duplicate, partial, or mismatched responses fail closed.

## Handler resolution

The worker runtime owns a static/server-controlled handler map:

```text
worker_handler_id -> reviewed handler implementation
```

Rules:

- unknown handler ID: fail before execution;
- duplicate handler ID: startup/configuration error;
- handler implementation/build identity must match the `ToolSpecV1` authority material;
- no `eval`, dynamic import from request data, pickle, cloudpickle, source-string execution, shell interpolation, or arbitrary executable path from IPC;
- handler registration happens at trusted build/startup time only.

## IPC protocol

Use framed, bounded structured IPC. The initial implementation may use stdin/stdout pipes or a local socket, but the protocol must be deterministic and resistant to framing abuse.

Required properties:

- strict UTF-8 JSON envelope;
- explicit maximum request/response bytes;
- one request / one terminal response per worker invocation unless a later protocol version explicitly defines streaming;
- length/framing validation before JSON parsing where practical;
- reject duplicate/unknown fields and non-finite numeric values;
- no unbounded stderr capture;
- stdout is protocol data only, not arbitrary handler logging;
- child logs are bounded and separately sanitized;
- request/response IDs must match exactly.

## Deadlines and cancellation

The parent owns the wall-clock deadline.

- launch with a new process group/session or platform-equivalent job boundary;
- on deadline/cancellation, terminate the entire descendant process tree, not only the direct worker;
- use a bounded graceful termination window followed by hard kill;
- reap the worker and descendants; do not leave zombies/orphans;
- record the stable termination class in ProofVault;
- late output after cancellation cannot turn the result into success.

Tests must include a sleeping tool, infinite loop, child-spawn tree, and worker that ignores graceful termination.

## Resource enforcement

### Baseline `subprocess_bounded_v1`

This profile provides process separation plus only the resource properties actually enforced by the host adapter. At minimum, production enablement requires:

- parent wall-clock deadline;
- process-tree kill/reap;
- request/output byte caps;
- bounded stderr/log capture;
- child exit/crash isolation;
- configured process-count control where the platform can enforce it;
- configured CPU/memory limits where the platform adapter can enforce them.

If CPU/memory/process limits requested by ToolSpec cannot be enforced, dispatch fails closed rather than pretending they are active.

`subprocess_bounded_v1` alone does **not** claim default-deny filesystem/network isolation.

### Capability-isolating profiles

Untrusted tools with meaningful filesystem/network/subprocess risk require an OS-backed profile whose enforcement is independently outside tool code. Examples may include a Linux namespace/seccomp/bubblewrap-backed adapter or a reviewed rootless OCI sandbox. Such profiles are separate versioned implementations and must advertise only capabilities they actually enforce.

A profile requiring default-deny network or filesystem access cannot be selected unless the runtime verifies the sandbox primitive is installed, configured, and functional. Missing primitives are a hard dispatch failure.

### Platform adapters

Keep parent control behind an explicit adapter interface so Linux, Windows, and other hosts do not silently diverge. Each adapter declares an immutable capability matrix, such as:

- wall deadline
- process-tree kill
- CPU limit
- memory limit
- process-count limit
- filesystem namespace/root isolation
- network deny/allowlist
- syscall/process-spawn restriction

Requested ToolSpec capabilities/limits must be a subset of the selected adapter/profile enforcement matrix.

## Filesystem capabilities

Consume the scoped filesystem capability model established by #40. The worker receives only server-derived sandbox mounts/handles or root IDs plus relative paths.

- no model/client supplied host root;
- read-only roots remain read-only at the OS boundary for capability-isolating profiles;
- write roots are explicit and minimal;
- symlink/device/special-file/root escape rules remain fail-closed;
- temporary workspace is bounded and isolated where the profile claims it;
- side-effect evidence records hashes/sizes/paths relative to approved capability roots, not raw file contents by default.

## Network capabilities

Default is no network authority for profiles that claim network isolation. If a ToolSpec requires network access:

- authority binds the exact network capability/allowlist policy identity;
- sandbox configuration is derived server-side;
- direct DNS/IP/proxy escape routes are considered in the profile threat model;
- no request-supplied proxy/env expansion silently broadens access;
- inability to enforce the declared policy fails closed.

A plain subprocess profile must not claim network denial merely because application code intends not to call the network.

## Environment and secrets

Build a minimal explicit worker environment.

- do not inherit the full parent environment;
- pass only allowlisted locale/runtime variables and opaque secret references explicitly authorized by ToolSpec;
- resolve secrets as late and narrowly as the chosen worker/sandbox model permits;
- never include secret values in WorkerRequest evidence, command-line arguments, process titles, logs, or exception messages;
- clear or discard temporary secret material on worker teardown where practical.

## Parent execution state machine

```text
VALIDATE_AUTHORITY
  -> BUILD_REQUEST
  -> VERIFY_PROFILE_ENFORCEMENT
  -> LAUNCH_WORKER
  -> RUNNING
     -> RESPONSE_RECEIVED
     -> TIMEOUT
     -> CANCELLED
     -> RESOURCE_LIMIT
     -> WORKER_CRASH
  -> VALIDATE_RESPONSE
  -> VALIDATE_OUTPUT_SCHEMA
  -> VALIDATE_POSTCONDITIONS
  -> APPEND_PROOFVAULT_EVIDENCE
  -> SUCCESS | FAIL_CLOSED
```

There is no path from worker launch/return directly to `SUCCESS`.

## ProofVault evidence

Append bounded authority events using #15. Minimum lifecycle events should permit reconstruction without storing private content:

- dispatch attempted/denied with authority identities;
- worker launched with isolation profile/build identity and requested limits;
- terminal worker classification;
- output digest/size and schema result;
- postcondition result/side-effect evidence digest;
- cancellation/timeout/resource enforcement result;
- final success/failure classification.

Full diagnostics, if retained, follow the ToolSpec evidence/redaction policy and must be bounded. A sanitized diagnostic digest may be stored instead of the body.

## Compatibility

The existing in-process Kitaev execution path may remain only as an explicitly named trusted/development execution class. It must not be the default for untrusted tools, and documentation/UI must not call it physical isolation.

During migration, the governance penalty logic may continue consuming stable worker failure classes. It cannot convert an isolation/protocol failure into success.

## Required adversarial coverage

At minimum:

- request serialization deterministic across restart;
- unknown/changed handler/spec/build/action identity rejected before launch;
- raw callable/module/import path in request impossible or rejected;
- malformed/oversized/deep/non-finite request rejected;
- worker output flooding hits parent cap without unbounded allocation;
- infinite loop and long sleep hit wall deadline and process tree is killed;
- child-spawn tree is terminated/reaped;
- worker crash does not terminate/corrupt orchestrator;
- requested unenforceable CPU/memory/process limit fails closed;
- memory/CPU/process-budget violation classified correctly on adapters that claim enforcement;
- capability-isolating profile denies unauthorized filesystem/network/process activity;
- missing sandbox primitive produces `ISOLATION_UNAVAILABLE`, not fallback;
- cancellation race cannot produce success after cancel;
- response request-ID/spec/action mismatch rejected;
- output schema/size/postcondition failure cannot report success;
- ProofVault evidence is chain-verifiable and contains authority/resource hashes/metadata but not secret/file bodies/private reasoning;
- restart after prior worker crash/timeout leaves authority ledger valid.

## Integration sequence

1. Merge #40 / PR #47 and update this branch onto that exact `main` head.
2. Introduce versioned WorkerRequest/WorkerResponse protocol and server-owned handler registry consumption.
3. Implement truthful baseline subprocess resource boundary with external deadlines/process-tree termination/output caps and platform capability matrix.
4. Wire Orchestrator governed dispatch to #16 and eliminate production direct-call fallback.
5. Add at least one OS-backed capability-isolating profile before claiming filesystem/network sandbox enforcement for untrusted tools.
6. Bind terminal output/postcondition/side-effect evidence to ProofVault.
7. Run full CI/CodeQL/GHAS plus platform-specific isolation integration tests.

## Exit criterion

Production governed execution never dispatches an arbitrary callable; exact approved ToolSpec/action/handler/build/isolation identities are revalidated before launch; deadlines/process-tree termination/output limits are enforced externally; requested resource/capability restrictions either have an enforcing backend or fail closed; worker crashes cannot take down the orchestrator; output/postconditions/evidence are verified before success; and documentation describes each isolation profile only to the level independently enforced by that profile.