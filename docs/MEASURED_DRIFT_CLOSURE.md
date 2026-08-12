# Measured Drift and Verified Closure Contract

Status: implementation contract for issue #17.

Exact base: `df59e00dc780c0bc27d823611d93f206b1cb1f68` (`main` after #20 / PR #50).

Dependencies already merged:
- #15 authoritative ProofVault evidence path;
- #40 immutable ToolSpecV1 / action authority;
- #16 bounded worker execution boundary;
- #20 immutable server-derived PolicyExecutionContext and fail-closed policy authority.

This contract replaces synthetic successful-step descent as the source of runtime drift/closure authority. It does not prohibit ELFE/Lyapunov mathematics as a controller or analytical model; it prohibits using the controller equation to manufacture evidence that the task became lawful.

## Root invariants

1. **Drift is observed, not awarded.** A successful tool call, elapsed step, low exception penalty, or approved model proposal does not reduce task-state drift unless an independent state/constraint evaluator observes a corresponding lawful-state improvement.
2. **Current drift is not historical penalty.** Instantaneous lawful-state distance is distinct from accumulated tool/provider/policy/reputation diagnostics.
3. **Unknown is not zero.** Any required component that cannot be measured is `UNMEASURED`; it cannot silently become `0.0` and cannot satisfy closure.
4. **Executor is not validator.** Handler return value, model assertion, or `success=True` is not independent postcondition evidence.
5. **Closure is a predicate over authoritative evidence.** Numerical tolerance alone cannot confer `ISOMORPHIC_CLOSURE`.
6. **No caller-provided scalar grants authority.** Client/model supplied `drift`, `approved`, lane, completion, or closure claims are non-authoritative model claims only.
7. **T_MAX expiry is not convergence.** Step/deadline exhaustion remains a terminal violation/stall even if a later surrogate calculation would cross a threshold.
8. **Prediction and measurement are distinct.** Preview may expose `PREDICTED` drift/risk, but execution closure must use `MEASURED` observations/evidence.
9. **Evidence is bounded and privacy-safe.** ProofVault stores hashes, finite bounded metrics, identities, rule IDs and evidence references—not private chain-of-thought, raw prompts, unbounded outputs, or secret/file bodies.
10. **Claims match proof.** A fixed-time guarantee may be emitted only when the discrete runtime recurrence/metric/domain assumptions are covered by a matching stability certificate. Otherwise the runtime says `UNVERIFIED_CONVERGENCE` or `BOUNDED_STEP_NO_CLOSURE`.

## Canonical state observation

Introduce immutable/versioned `StateObservationV1` (or an equivalent frozen type) created server-side from authoritative runtime facts.

Minimum fields:
- `schema_version`;
- `trace_id`, `correlation_id`, `step_index`;
- observation phase: `BEFORE` or `AFTER`;
- exact `tool_id`, `tool_contract_hash`, `action_digest` where applicable;
- exact worker result/status identity and result digest/size, never raw unbounded result bodies in the observation authority record;
- exact policy decision/context/bundle hashes from #20;
- postcondition validator identity/version and pass/fail/unknown state;
- bounded side-effect evidence digests/relative capability paths where declared by ToolSpec;
- provider identity plus bounded uncertainty/health facts where measurable;
- resource/deadline observations such as elapsed time, remaining deadline, resource-limit result and isolation enforcement identity;
- domain state/evidence references required by the registered evaluator;
- observation hash and evaluator-compatible state identity.

All fields must be finite, bounded, cycle-safe, deterministic and canonicalizable. Authority identifiers are rejected when overlong rather than truncated into collisions.

## Constraint evaluator registry

Define a server-owned immutable registry:

`evaluator_id + evaluator_version + evaluator_build_identity -> ConstraintEvaluator`

The evaluator is selected by trusted runtime/domain configuration, never by model/client import path or source text.

A `ConstraintEvaluator` consumes authoritative before/after observations plus exact ToolSpec/action/result evidence and returns a bounded immutable `ConstraintAssessmentV1`.

Minimum assessment fields:
- evaluator identity/version/build hash;
- domain/metric version;
- required-component measurement states;
- normalized component measurements;
- postcondition result and rule IDs;
- evidence references/digests;
- lawful-state projection/target identity where the domain defines one;
- threshold/tolerance identity where applicable;
- assessment hash.

If no matching evaluator exists, or its required evidence is unavailable/invalid, the result is `UNVERIFIED_NO_CLOSURE`; do not run a universal synthetic closure fallback.

## DriftVectorV1

Refactor/reuse `drift.py` rather than creating a third unrelated drift system. Split it into:

1. **Instantaneous measured drift** for authority/policy/closure.
2. **Historical drift integral/report** for diagnostics/reputation/trend analysis.

Canonical instantaneous components should include at least:
- `constraint` / lawful-state distance;
- `postcondition` error;
- `execution_error`;
- `policy` / compliance state;
- `provider_uncertainty`;
- `resource_latency`.

Each component has a measurement state (`MEASURED` or `UNMEASURED`) and, when measured, a finite normalized value in the documented range (normally `[0,1]`). Missing components are not coerced to zero.

A versioned `DriftMetricIdentity` binds:
- component set/version;
- normalization rules;
- deterministic weights/composition rule;
- required components for authoritative closure;
- evaluator identity/version;
- threshold/tolerance identity;
- metric implementation/build identity.

If a scalar composite is needed for routing/policy compatibility, compute it deterministically from the measured vector and bind the metric identity. The scalar is a projection of evidence, not the source of evidence.

Historical `DriftReport` may integrate validated instantaneous measurements over time, but accumulated penalties may not be reused as current task-state distance.

## Thermodynamics / ELFE migration

`SystemThermodynamics.apply_drift_update(step_count, error_penalty)` must cease being the production authority path for task-state drift.

Required migration:
- initialize current authoritative drift from the first valid measured observation/assessment, not a universal `1.0` progress counter;
- update current drift from subsequent measured `DriftVectorV1` values;
- tool/error penalties may remain diagnostic perturbation inputs or historical integrals, but cannot directly grant constraint-drift descent;
- preserve the old synthetic recurrence only behind an explicitly named legacy/model/testing compatibility path if existing APIs require it;
- remove unconditional universal `_thoth_wadjet_closure()` authority. A snap/tolerance is only valid when a domain metric/certificate explicitly defines that equivalence class;
- exact floating equality to zero is never a sufficient closure predicate.

## Verified closure predicate

Introduce an immutable/versioned `ClosureDecisionV1` and server-owned closure evaluator.

`ISOMORPHIC_CLOSURE` requires all applicable conditions:
- authoritative current observation is `MEASURED`, not preview/predicted;
- required drift components are measured;
- measured constraint distance is within the exact domain threshold/tolerance identity;
- independent required postconditions passed;
- exact validator/evaluator identity and evidence are present;
- current #20 policy decision is ALLOW and policy evidence is persisted;
- no unresolved worker/execution/resource/isolation failure;
- required side-effect evidence/postconditions from ToolSpec are satisfied;
- the evidence chain is persisted successfully before final closure;
- if a fixed-time guarantee is claimed, a matching valid stability certificate covers this metric/evaluator/domain/runtime configuration.

Otherwise emit a distinct non-closure state such as:
- `UNVERIFIED_NO_CLOSURE` — evaluator/evidence unavailable;
- `UNVERIFIED_CONVERGENCE` — measurement exists but fixed-time proof/certificate does not;
- `BOUNDED_STEP_NO_CLOSURE` — bounded controller/execution completed without verified closure;
- `STALLED` — progress predicate/oscillation policy says no validated progress;
- `T_MAX_VIOLATION` — authoritative step/wall budget expired;
- existing fail-closed policy/execution statuses where applicable.

A terminal `T_MAX_VIOLATION`, policy denial, evidence failure or worker failure can never later be relabeled as closure by a numeric snap.

## Stability certificate

If fixed-time convergence remains a production guarantee, define immutable `StabilityCertificateV1` bound to:
- metric identity/version;
- evaluator/domain identity/version;
- admissible initial-state set/assumptions;
- actual discrete sampling/update interval;
- actual recurrence/controller implementation identity;
- coefficients `a,b,p,q` and any descent scale;
- perturbation/error bound;
- threshold/tolerance definition;
- oscillation/chattering handling;
- proven or calibrated maximum steps/wall time;
- certificate/proof/calibration artifact digest.

The continuous-time expression `Vdot <= -a V^p - b V^q` and its analytical bound are not, by themselves, a certificate for the repository's discrete recurrence, clamping, perturbation, snap and scheduler timing.

Certificate mismatch/staleness yields `UNVERIFIED_CONVERGENCE`, not a silently retained guarantee.

## Progress, oscillation and stall

Define progress from measured before/after state, not successful call count.

At minimum:
- no-op success with unchanged measured state does not reduce constraint drift;
- wrong-state success may preserve or increase measured drift;
- validated corrective change reduces only the components supported by evidence;
- repeated oscillation/chattering is detected from measured observations and may escalate lane, damp/control, or stall according to a versioned policy;
- a missing measurement cannot be interpreted as progress.

## LaneRouter correction

`lanes.py` must stop allowing a caller-supplied `drift == 0.0` to jump directly to AUTHORITATIVE or to create closure.

Replace `advance(approved: bool, drift: float)` as the production authority API with an immutable server-derived `LaneTransitionEvidenceV1` (legacy wrapper may remain non-authoritative).

The evidence should bind:
- measured drift/closure decision identity;
- independent validator/postcondition state;
- #20 policy decision/context/bundle hash;
- execution intent/action identity where required;
- ProofVault evidence reference/hash;
- deadline/step state;
- prior/current lane and transition rule identity.

No REFLEX -> AUTHORITATIVE shortcut may occur solely from a scalar. AUTHORITATIVE output does not itself imply closure; final closure comes only from `ClosureDecisionV1`.

## Orchestrator integration

Production execution sequence:

`PREVIEW/PREDICTION -> POLICY/APPROVAL -> BEFORE_OBSERVATION -> BOUNDED_ACTUATION -> OUTPUT/POSTCONDITION VALIDATION -> AFTER_OBSERVATION -> CONSTRAINT ASSESSMENT -> DRIFT MEASUREMENT -> POLICY/RESOURCE CONSISTENCY CHECK -> CLOSURE DECISION -> EVIDENCE -> TERMINAL STATUS`

Required properties:
- preview remains zero-actuation and drift is explicitly `PREDICTED`;
- before/after observations are server-derived and tied to the exact approved action;
- executor result cannot self-certify postconditions;
- #20 PolicyExecutionContext consumes the measured drift vector/composite from the same authoritative observation rather than a caller/model scalar;
- final success/closure requires all mandatory evidence persistence.

## ProofVault evidence

Add bounded lifecycle evidence sufficient for independent reconstruction:
- `state.observation.before` / `state.observation.after`;
- `constraint.assessment`;
- `drift.evaluation`;
- `closure.decision`;
- `stability.certificate` identity/result when used;
- `lane.transition` where authoritative routing changes.

Evidence stores hashes, bounded finite metrics, versions, rule IDs, statuses and references. Raw prompts/private reasoning/unbounded result bodies/secrets are excluded.

## Graph and compatibility surfaces

`graph_elve.py`, `lanes.py`, and old closure tests currently inherit the synthetic scalar semantics. Do not leave an alternate public execution path that can still emit authoritative closure from `apply_drift_update()` or `drift == 0.0`.

Either migrate those paths to the same measured drift/closure contract in this PR or explicitly classify/disable them as legacy/experimental until issue #39 is implemented. They may not retain production closure authority.

## Required adversarial coverage

At minimum:
- successful no-op repeated 100 times does not reduce measured constraint drift or close;
- manually supplying `drift=0`, negative, NaN/Inf or fabricated `approved=True` cannot force AUTHORITATIVE closure;
- executor reports success while independent evaluator observes unchanged/wrong state -> no closure;
- measured corrective state change deterministically reduces only supported components;
- missing evaluator/required component -> `UNVERIFIED_NO_CLOSURE`;
- low constraint/postcondition drift with unsafe resource/policy/execution component -> no closure;
- stale/mismatched evaluator/metric/stability certificate -> fail closed/no fixed-time claim;
- preview predicted drift cannot be replayed as measured closure evidence;
- T_MAX expiry remains violation and cannot be overwritten by later snap/measurement;
- replay/restart from identical observations/evaluator/metric versions reproduces the same drift and closure hash;
- evidence persistence failure prevents closure;
- oscillation/chattering does not manufacture progress;
- legacy scalar LaneRouter shortcut cannot grant authority;
- old `tests/test_isomorphic_closure.py` assertions that successful steps automatically lower drift are replaced by specification tests over measured observations.

## Documentation truth

Until a matching discrete stability certificate exists, documentation must describe ELFE as a bounded-step/remediation controller or analytical target, not an enforced fixed-time convergence guarantee for all tasks.

## Exit criterion

No production path emits `ISOMORPHIC_CLOSURE` because successful steps, elapsed time, error-free tool returns, a universal numeric snap, or caller-supplied scalar drift reached zero. Closure is reproducibly derived from exact measured state/constraint/postcondition evidence under a versioned evaluator/metric/policy context, persisted through ProofVault, with fixed-time claims limited to configurations covered by a matching discrete stability certificate.