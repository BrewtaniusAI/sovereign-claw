# Sovereign Claw — Architecture Map
## GOD FILE v∞.1 → Python Symbol Reference

This document provides a transparent audit trail linking every
mathematical primitive in the GOD FILE v∞.1 specification to its
concrete Python implementation.  Researchers and enterprise auditors
can verify each equation is faithfully encoded by cross-referencing
the symbol table below with the source modules.

---

## 1. Universal Intent Layer (UIL) — Constraint Manifold

**Formal specification**

| Symbol | Definition |
|--------|-----------|
| `Φ(x)` | Constraint potential for system state `x` |
| `C(x) = argmin_{y ∈ X} Φ(y)` | Nearest lawful state (gravity well attractor) |
| `D(x) = ‖x − C(x)‖` | System drift — distance from lawful attractor |

**Python mapping**

| Concept | Module | Symbol |
|---------|--------|--------|
| Constraint manifold | `thermodynamics.py` | `TaskManifold` |
| Forbidden-action constraint set | `thermodynamics.py` | `TaskManifold.forbidden_actions` |
| Step budget constraint | `thermodynamics.py` | `TaskManifold.t_max_steps` |
| System drift D(x) | `thermodynamics.py` | `SystemThermodynamics.current_drift` |
| Isomorphic closure (D→0) | `orchestrator.py` | `Status.ISOMORPHIC_CLOSURE` |

**Invariant**: The Orchestrator never takes an action that increases
`current_drift` intentionally.  All increases are error penalties
translated by the Kitaev shield.

---

## 2. ELFE v∞.1 Fixed-Time Lyapunov Kernel

**Formal specification**

The fixed-time Lyapunov condition:

```
dV(x)/dt ≤ −a·V(x)^p − b·V(x)^q
```

Parameters: `a, b > 0`,  `0 < p < 1`,  `q > 1`

Bounded settling time guarantee:

```
T_max ≤ 1/(a(1−p)) + 1/(b(q−1))
```

**Python mapping**

| Concept | Module | Symbol |
|---------|--------|--------|
| Lyapunov coefficients a, b | `thermodynamics.py` | `TaskManifold.elfe_a`, `elfe_b` |
| Exponents p, q | `thermodynamics.py` | `TaskManifold.elfe_p`, `elfe_q` |
| Dual-regime descent step | `thermodynamics.py` | `SystemThermodynamics.apply_drift_update()` |
| T_max analytical bound | `thermodynamics.py` | `TaskManifold.theoretical_t_max` |
| T_max enforcement | `thermodynamics.py` | `SystemThermodynamics.check_isomorphic_state()` |
| Hard Silence Clause (T_max breach) | `orchestrator.py` | `Status.T_MAX_VIOLATION` |

**Discretisation note**: The continuous Lyapunov condition is
discretised per step as:

```python
descent = a * (drift ** p) + b * (drift ** q)
new_drift = clamp(drift - descent + error_penalty, 0.0, 1.0)
```

Default parameters (a=1, b=1, p=0.5, q=2) give `theoretical_t_max = 3.0`
steps.  Increase `t_max_steps` beyond this for real tasks.

---

## 3. Thoth-Wadjet Closure (Asymptotic Truncation)

**Formal specification**

```
If D(x) ≤ 1/64,  then D(x) → 0
```

Prevents infinite micro-correction loops once the agent is
mathematically close enough to the lawful state.

**Python mapping**

| Concept | Module | Symbol |
|---------|--------|--------|
| Snap threshold (1/64) | `thermodynamics.py` | `_THOTH_SNAP = 1/64` |
| Snap function | `thermodynamics.py` | `SystemThermodynamics._thoth_wadjet_closure()` |
| Applied after every step | `thermodynamics.py` | called inside `apply_drift_update()` |

---

## 4. Kitaev Zero-Mode Shielding (Execution Isomorphism)

**Formal specification**

Fibonacci R-Matrix (topological gap):

```
R = [[e^{−iπ/5},    0        ],
     [0,            e^{i4π/5}]]
```

Hamiltonian topological gap: `λ ≈ 0`

Error translation: `Error → ΔΦ` (drift penalty, not stack trace)

**Python mapping**

| Concept | Module | Symbol |
|---------|--------|--------|
| Zero-mode sandbox | `kitaev_shield.py` | `KitaevZeroMode.execute_safely()` |
| Error → drift penalty | `kitaev_shield.py` | `_penalty_for(exc)` + tiered `_PENALTY_MAP` |
| Stack trace isolation | `kitaev_shield.py` | `_internal_trace` key (never returned to LLM) |
| Penalty scale tuning | `kitaev_shield.py` | `KitaevZeroMode.penalty_scale` |
| Shield-level forbidden check | `kitaev_shield.py` | `KitaevZeroMode.forbidden_names` |
| Drift penalty application | `orchestrator.py` | `therm.apply_drift_update(error_penalty=...)` |

**Invariant**: The string returned to the LLM in `payload` on error
never contains a Python traceback, OS error code, or raw exception
message.  It contains only: `"Constraint blocked: tool '<name>'
encountered <ErrorType>. Recalculate approach vector."`

---

## 5. Byzantine Reputation Weighting (Cybernetic Isomorphism)

**Formal specification**

Historical drift integral per agent:
```
R_i = ∫₀ᵀ D_i(t) dt
```

Agent reputation weight (exponential decay):
```
w_i = e^{−k·R_i}
```

Bayesian state update:
```
P(H | S₁..Sₙ) ∝ P(H) · ∏ᵢ P(Sᵢ | H)^{wᵢ}
```

**Python mapping**

| Concept | Module | Symbol |
|---------|--------|--------|
| Drift integral R_i (discrete sum) | `proof_vault.py` | `agent_reputation.drift_integral` |
| Reputation weight w_i | `proof_vault.py` | `ProofVault.get_agent_reputation_weight(k)` |
| Per-step integral update | `proof_vault.py` | `ProofVault.update_agent_reputation()` |
| All-agent leaderboard | `proof_vault.py` | `ProofVault.list_agent_weights()` |
| Orchestrator triggers update | `orchestrator.py` | `self.vault.update_agent_reputation(agent_id, penalty)` |
| agent_id injection | `backends_ollama.py` | `decision["agent_id"] = "rabbit" / "cypher"` |
| Giles agent_id | `backends_giles.py` | `decision["agent_id"] = "giles"` |

**Extension point**: Full Bayesian state update (the ∏ formula) is
the natural next layer.  The `w_i` values from `list_agent_weights()`
can be used as weights when merging multi-agent history entries in a
custom `LLMBackend`.

---

## 6. Proof Vault (Immutable WORM Ledger)

**Formal specification**

Local append-only ledger.  Every decision, drift value, tool call
result, and governance action is written before the next action is
taken.  No record is ever modified or deleted.

**Python mapping**

| Concept | Module | Symbol |
|---------|--------|--------|
| Trace creation | `proof_vault.py` | `ProofVault.create_trace()` |
| Step append | `proof_vault.py` | `ProofVault.append_step(StepRecord)` |
| Immutability | `proof_vault.py` | No UPDATE/DELETE statements anywhere |
| Thread safety | `proof_vault.py` | WAL mode + `check_same_thread=False` |
| Giles authoritative seal | `graph_elve.py` | `giles_node()` → `vault.append_step(GATA_PRIME_SEAL)` |
| Orchestrator step log | `orchestrator.py` | `Orchestrator._log_step()` |
| Drift analytics | `proof_vault.py` | `ProofVault.get_trace_summary()` |
| Environment-configurable DB path | `proof_vault.py` | `SOVEREIGN_CLAW_DB` env var |

---

## 7. Tri-Temporal Execution Governor (Three-Lane Architecture)

**Formal specification**

| Lane | Name | Agent | Guarantee |
|------|------|-------|-----------|
| 1 | Reflex | Kitaev sandbox | Sub-second, isolated from LLM context |
| 2 | Deliberate | Rabbit → Cypher loop | Bounded by `MAX_LOOPS` stall guard |
| 3 | Authoritative | Giles | Sealed, cryptographically logged |

No-skip invariant: execution must traverse 1 → 2 → 3 in order.
Short-circuit to Lane 3 permitted only when `D(x) = 0`.

**Python mapping**

| Concept | Module | Symbol |
|---------|--------|--------|
| Lane enum | `lanes.py` | `Lane.REFLEX / DELIBERATE / AUTHORITATIVE / STALL` |
| No-skip invariant | `lanes.py` | `LaneRouter.advance()` |
| Stall guard | `lanes.py` | `LaneRouter.max_deliberate_loops` |
| Early closure | `lanes.py` | `advance(drift=0.0)` → AUTHORITATIVE |
| LangGraph wiring | `graph_elve.py` | `rabbit_node → cypher_node → router_node → giles_node` |
| Swarm coordinator | `examples/05_full_swarm_demo.py` | `SwarmCoordinator` |

---

## 8. Biological Isomorphism (Memory Scaling Law)

**Formal specification**

Input-to-Governance scaling:
```
S ∝ G^{2/3}
```

Where:
- `S` = Information Surface Area (input context size)
- `G` = Governance Volume (system's internal routing capacity)

If `S` exceeds `G`, the agent loses temporal bounds (Silence Clause fires).

**Python mapping**

| Concept | Module | Symbol |
|---------|--------|--------|
| Context growth proxy | `orchestrator.py` | `len(history)` grows each step |
| Governance capacity | `thermodynamics.py` | `TaskManifold.t_max_steps` (hard cap on G) |
| Thermodynamic ceiling trigger | `orchestrator.py` | Soft Silence Clause at `risk_threshold` |

**Hardware note**: The S ∝ G^{2/3} law predicts that large swarms
(>100 agents, >100k-token contexts) will exceed standard PC RAM.
The Sovereign Node (40Gbps / 80Gbps CXL 3.0 drives) is the
intended physical substrate for this tier.

---

## 9. Silence Clause (Automated Compliance Mechanism)

Two variants:

| Variant | Trigger | Python location |
|---------|---------|----------------|
| Hard Silence | `step_count >= t_max_steps` | `orchestrator.py` pre-loop check |
| Soft Silence | `new_drift > risk_threshold` | `orchestrator.py` post-step check |
| Stall Guard | `loop_count >= MAX_LOOPS` | `graph_elve.py` router + `lanes.py` LaneRouter |

All variants write a final `StepRecord` to ProofVault before halting,
ensuring the sealed state is always recoverable.

---

## Audit Checklist

To verify a deployment is spec-compliant, confirm:

- [ ] `TaskManifold.theoretical_t_max` ≤ `t_max_steps` (or justified deviation)
- [ ] No `UPDATE` or `DELETE` SQL in `proof_vault.py`
- [ ] `KitaevZeroMode.execute_safely()` is the only path for tool execution
- [ ] `_internal_trace` never appears in any string passed to `llm.decide_next_action()`
- [ ] `ProofVault.update_agent_reputation()` called after every tool execution
- [ ] All terminal states (`ISOMORPHIC_CLOSURE`, `T_MAX_VIOLATION`, `HALTED_SILENCE_CLAUSE`) write a final `StepRecord`
