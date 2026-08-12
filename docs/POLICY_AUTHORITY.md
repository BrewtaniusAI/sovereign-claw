# Policy Authority Contract

Status: implementation contract for issue #20. This branch starts from exact merged `main` `efacd28ba88f7caca0d2c99c6459c703147394d2`, which already contains the ToolSpec authority and bounded execution boundary from #40/#16.

## Purpose

Make policy evaluation a reproducible, fail-closed authority boundary. Local deterministic rules, optional OPA/Rego, guardrail BLOCK rules, execution context, and policy evidence must compose into one deny-dominant decision before actuation. No configured authoritative policy dependency may silently disappear.

## Root invariants

1. **Authority is explicit input, not hidden mutable state.** Policy evaluation consumes an immutable server-derived context. Model/client supplied trace IDs, approvals, identities, budgets, drift, tool privilege, or policy metadata are never trusted as authority.
2. **Deny dominates.** Local DENY, guardrail BLOCK, external authoritative OPA DENY, or authoritative policy unavailability all produce final DENY.
3. **No default allow from malformed external policy.** Missing OPA, timeout, nonzero exit, empty result, empty expressions, undefined/missing value, missing `allow`, wrong result type, malformed JSON, oversize output, or policy-load error fail closed in authoritative mode.
4. **Advisory mode is explicit.** OPA may be advisory only through an explicit typed configuration. Advisory failures/denials are labeled and evidenced; they never masquerade as authoritative allow.
5. **Execution context is reproducible.** A verifier can recompute a decision from the recorded canonical ExecutionContext hash, PolicyBundleIdentity, rule inputs, and exact policy artifact digests without hidden process state.
6. **Policy subprocesses are bounded.** OPA receives finite bounded canonical JSON and runs with a hard timeout, bounded stdout/stderr, explicit environment/cwd, and no shell.
7. **Policy evidence is mandatory for governed execution.** Final policy decision evidence must be persisted through ProofVault before privileged actuation can proceed. Logging is diagnostic only.
8. **Learned signals do not silently become authority.** Process-local violation history cannot change allow/deny after restart. Keep learned signals advisory unless persisted/versioned/hashed as part of the policy bundle.
9. **Guardrails consume authority, not caller claims.** Privileged approval, budget, action history, principal scopes, and tool risk/capabilities come from server-owned execution state, ToolSpec, UsageTracker, and recorded execution intent/operator approval.
10. **Bound all policy-facing data.** Context, reasons, matched rule IDs, OPA diagnostics, hashes, and evidence have explicit size/count/depth limits and finite canonical serialization.

## Immutable PolicyExecutionContext

Introduce a frozen/versioned policy context or equivalent immutable mapping whose authority fields include, at minimum:

- schema/context version
- trace/session/correlation identity from the runtime
- authenticated principal identity and scopes
- policy profile
- requested tool ID, ToolSpec contract hash, risk class and capabilities
- live measured drift components available at decision time; until #17 replaces scalar drift, carry the current scalar explicitly and label its semantics
- lane/role identity
- provider/fallback policy identity where relevant
- budget/resource state from authoritative usage/runtime objects, not model input
- config/runtime identity hash
- execution-intent/approval correlation identity when present
- remaining execution deadline / policy timeout budget
- action/tool-call counters from runtime state

The context must have deterministic finite canonical serialization and `context_hash = SHA256(canonical_context_bytes)`.

Legacy callers may use a compatibility adapter, but authoritative evaluation must not depend on `PolicyEngine._current_drift` or other hidden mutable state.

## PolicyBundleIdentity

Define a deterministic bundle identity containing:

- local evaluator version
- local rule/profile configuration hash
- exact policy profile/version
- OPA mode: `disabled | authoritative | advisory`
- OPA query identity
- OPA policy directory/bundle content digest when configured
- OPA evaluator/version identity where available
- guardrail bundle/version/hash if guardrails participate
- learned-signal mode and root/version; default authoritative value should be `advisory/none` unless persisted

`policy_bundle_hash = SHA256(canonical_policy_bundle_identity)`.

Directory hashing must be deterministic: stable relative paths, file bytes, bounded file count/size, reject unreadable/symlink-escape/special-file ambiguity rather than inventing a hash.

## Decision model

Use stable classes, for example:

- `ALLOW`
- `POLICY_DENY`
- `POLICY_UNAVAILABLE`
- `POLICY_INPUT_INVALID`
- `POLICY_INFRA_FAILURE`

A final `PolicyDecision` should expose at least:

- allowed
- stable decision/failure class
- bounded reasons
- bounded matched policy IDs
- profile
- context hash
- policy bundle hash
- OPA mode/status when configured
- optional bounded evaluator metadata needed for verification

Composition is deny-dominant:

```text
local_result = ALLOW | DENY
external = DISABLED | ALLOW | DENY | UNAVAILABLE
final = DENY if local DENY
        DENY if authoritative external DENY
        DENY if authoritative external UNAVAILABLE
        otherwise ALLOW
```

An explicit external `{allow:false, deny:[]}` MUST still deny.

## OPA/Rego contract

When configured, support one strict documented Rego result schema:

```json
{
  "allow": true,
  "deny": [],
  "matched": []
}
```

Rules:

- result value must be an object
- `allow` is required and must be boolean
- `deny` and `matched` default only if explicitly permitted by schema; if present they must be bounded lists of bounded strings
- scalar boolean results are rejected unless a future protocol version explicitly supports them
- empty/missing result/expressions/value is unavailable, never allow
- malformed/oversize/non-finite output is unavailable
- explicit `allow:false` always denies regardless of reason list

### Bounded runner

The OPA runner must:

- locate the binary before launch; missing binary is `POLICY_UNAVAILABLE` in authoritative mode
- use no shell
- use a minimal allow-listed environment
- use an explicit safe working directory
- pass canonical finite bounded input bytes
- derive timeout from both configured maximum and remaining execution deadline
- bound stdout/stderr independently and terminate on overflow/timeout
- sanitize diagnostics to stable class + bounded message; do not place arbitrary stderr in model/user context
- handle `TimeoutExpired`, JSON decode/type errors and OS/subprocess failures without escaping the authority boundary

## Violation / learned-signal handling

Policy infrastructure failures are not tool violations. `POLICY_UNAVAILABLE` / `POLICY_INFRA_FAILURE` must not increment learned tool-deny counters.

If violation history remains:

- populate real timestamps
- hard-bound tool cardinality/history size
- return immutable/copy-safe diagnostics
- keep learned auto-deny advisory by default, or persist/version/hash it before it can affect authoritative decisions

## Guardrail integration

If `GuardrailEngine` participates in production authority:

- derive ToolSpec privilege/capabilities from the governed registry
- derive authenticated principal and grants from server identity/access state
- derive human approval from recorded one-time execution intent/operator approval evidence
- derive budget/usage from authoritative budget/UsageTracker state
- derive action history from runtime/session/trace state
- a missing/errored BLOCK rule fails closed in authoritative mode
- custom BLOCK rules with no implementation cannot silently pass
- guardrail rule/version/hash and decision are included in PolicyBundleIdentity/evidence

Do not trust request fields such as `authorized_privileged_tools`, `human_approved`, caller-provided cost/token totals, or caller-supplied action history as proof.

## Orchestrator integration

Immediately before every governed decision that can lead to actuation:

1. Build the immutable server-derived PolicyExecutionContext from the current authoritative runtime state.
2. Evaluate local + guardrail + optional OPA policy against that exact context.
3. Persist bounded `policy.decision` authority evidence through ProofVault containing hashes/identities, stable decision class, matched IDs and sanitized reasons.
4. If evidence persistence fails, fail closed before worker launch.
5. Only an ALLOW decision may proceed to the #40 ToolSpec / #16 worker authority checks.

Preview must remain zero-actuation. Preview may evaluate policy against a preview-labeled immutable context, but cannot mutate learned authoritative state or create an approval-equivalent execution artifact.

## ProofVault evidence

Persist enough bounded evidence to reproduce/verify the policy decision without recording private reasoning or raw sensitive payloads:

- context hash + selected bounded non-sensitive context identities
- policy bundle hash + local/OPA/guardrail component hashes
- profile/mode/version
- final allow/deny + stable failure class
- matched rule IDs and bounded sanitized reasons
- OPA execution status/duration/output digest where configured
- approval/execution-intent correlation identity
- trace/correlation IDs

Raw OPA stderr, secrets, prompts, model outputs, file bodies and private reasoning are not evidence fields.

## Required adversarial tests

At minimum:

- explicit OPA `{allow:false, deny:[]}` denies
- `{allow:false, deny:[...]}` denies
- `{allow:true, deny:[]}` allows when local policy allows
- missing `allow`, scalar false/true, empty result, empty expressions, missing value, malformed JSON all fail closed in authoritative mode
- missing OPA binary, timeout, nonzero exit, stdout overflow, stderr overflow and policy-directory read/hash failure fail closed in authoritative mode
- advisory mode is explicit and decision evidence labels it
- local deterministic rules work normally when OPA is disabled
- non-finite/deep/oversize policy context is rejected deterministically
- live drift from one execution does not leak into another; no hidden `_current_drift` authority dependency
- OPA DENY with empty reason cannot be lost by final composition
- policy infra failures do not poison learned tool-deny history
- learned process-local state cannot silently change authoritative decision across restart
- reasons/matched IDs are bounded/type-checked
- policy bundle hash changes when Rego content/profile/mode/evaluator identity changes
- same context + same bundle produces stable context/bundle hashes and decision across restart
- ProofVault evidence persistence failure prevents governed actuation
- preview policy evaluation remains zero-actuation and does not mutate authoritative learned state

## Exit criterion

Every governed actuation is preceded by a reproducible deny-dominant policy decision over an immutable server-derived context; configured authoritative OPA can never disappear or default-allow on failure/malformed output; explicit external deny is honored even without reason text; policy subprocesses and evidence are bounded; and the exact merge head passes full CI, both CodeQL analyzers, GHAS and repository security gates.
