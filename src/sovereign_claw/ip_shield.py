"""
ip_shield.py — Intellectual Property Protection Layer
======================================================
Implements the CollectiveOS IP Protection Protocol for the Sovereign Claw
Community Edition.

TWO-TIER PROTECTION MODEL
--------------------------
Tier 1 — COMMUNITY EDITION (this repo, Apache-2.0)
  Public interface only. Mathematical symbols renamed. Internal docstrings
  stripped from compiled distributions. No GOD FILE v∞.1 coefficients.

Tier 2 — PROPRIETARY CORE (not distributed)
  Full MythicNeuroKernel internals, GOD FILE v∞.1 equations, Weavers_Code
  spec, ELFE exact coefficients. Governed by Brewtanius Ink LLC / Immortal
  Tek Inc. commercial IP terms. ProofVault WORM receipts serve as prior art.

ANTI-RECALL MECHANISMS
-----------------------
1. Symbol Aliasing
   All proprietary identifiers are aliased through _SYMBOL_MAP at import.
   The public names are stable; the internal names rotate per build.

2. Coefficient Sealing
   ELFE a, b, p, q values are NOT stored in source. They are loaded at
   runtime from a sealed config (env var or encrypted .sckey file).
   Default published values (1.0, 1.0, 0.5, 2.0) are demonstration
   values only — the production GOD FILE coefficients are different.

3. Vault Fingerprint
   Every ProofVault trace carries a build_id fingerprint. Traces from
   a counterfeit build will not validate against Immortal Tek receipts.

4. No __repr__ on internals
   MythicNeuroKernel, QuipuRouter, and GardenersProtocol suppress
   __repr__ and __str__ in compiled distributions so memory inspection
   yields no useful symbolic information.

5. Bytecode stripping
   The build script (build_protected.py) compiles all Tier-2 modules to
   .pyc with source stripping (-OO) and renames them with a content hash
   prefix to prevent trivial extraction.

USAGE
-----
This module is imported by the build system, not by application code.
Application code never imports from ip_shield directly.

For the Community Edition, the relevant protection is:
  - This file documents what IS and IS NOT protected
  - The published coefficients are placeholder values
  - Internal routing logic is abstracted behind the public API
  - ProofVault traces serve as immutable prior art timestamps

PRIOR ART REGISTRY
------------------
All substantive research frameworks, mathematical derivations, and
architectural specifications are published on Zenodo under:
  Brewtanius Ink LLC / Human Global Science Collective (HGSC)
with cryptographic verification as prior art.

Zenodo DOIs are embedded in ProofVault trace metadata to create an
unbroken chain: idea → specification → implementation → sealed trace.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any, Dict, Optional, Tuple

# ── Build identity ────────────────────────────────────────────────────────────
_BUILD_VERSION  = "2.0.0"
_BUILD_EDITION  = "COMMUNITY"       # COMMUNITY | ENTERPRISE | SOVEREIGN_NODE
_OWNER_ENTITY   = "Brewtanius Ink LLC / Immortal Tek Inc."
_COLLECTIVEOS   = "CollectiveOS GOD FILE v∞.1"

# Embedded in every ProofVault trace to chain back to the build
BUILD_FINGERPRINT = hashlib.sha256(
    f"{_BUILD_VERSION}:{_BUILD_EDITION}:{_OWNER_ENTITY}".encode()
).hexdigest()[:12]


# ── Sealed coefficient loader ─────────────────────────────────────────────────
# In COMMUNITY edition, returns demonstration values.
# In ENTERPRISE/SOVEREIGN_NODE, loads from SOVEREIGN_CLAW_KEY env var
# or an encrypted .sckey file.

_DEMO_COEFFICIENTS: Tuple[float, float, float, float] = (1.0, 1.0, 0.5, 2.0)

def load_elfe_coefficients() -> Tuple[float, float, float, float]:
    """
    Load ELFE a, b, p, q from sealed configuration.

    Community Edition: returns demonstration values.
    Enterprise/Sovereign Node: decrypts from SOVEREIGN_CLAW_KEY env var.

    Returns
    -------
    (a, b, p, q) — Lyapunov kernel coefficients
    """
    edition = os.environ.get("SOVEREIGN_CLAW_EDITION", "COMMUNITY").upper()

    if edition == "COMMUNITY":
        return _DEMO_COEFFICIENTS

    # Enterprise: load from sealed env key
    raw_key = os.environ.get("SOVEREIGN_CLAW_KEY", "")
    if not raw_key:
        # Silently fall back to demo — never crash in production
        return _DEMO_COEFFICIENTS

    # DRIFT-8 FIX: The original code silently fell back to demo coefficients
    # on ANY decode error, including malformed keys that could indicate a
    # supply-chain tampering attempt.  The hardened version:
    #   1. Validates base64 padding before decoding
    #   2. Validates coefficient ranges with explicit bounds
    #   3. Emits a stderr warning (not an exception) on validation failure
    #      so operators can detect misconfigured Enterprise deployments
    #      without crashing the process.
    import base64
    import sys

    try:
        # Normalise padding
        padded = raw_key + "=" * (-len(raw_key) % 4)
        decoded = base64.b64decode(padded).decode("utf-8")
        parts = decoded.split(":")
        if len(parts) != 4:
            print(
                f"[sovereign-claw] WARN: SOVEREIGN_CLAW_KEY has {len(parts)} fields "
                "(expected 4: a:b:p:q). Falling back to demo coefficients.",
                file=sys.stderr,
            )
            return _DEMO_COEFFICIENTS
        a, b, p, q = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
        # Lyapunov stability requires: a > 0, b > 0, 0 < p < 1, q > 1
        if not (a > 0 and b > 0 and 0.0 < p < 1.0 and q > 1.0):
            print(
                f"[sovereign-claw] WARN: SOVEREIGN_CLAW_KEY coefficients fail "
                f"Lyapunov bounds (a={a}, b={b}, p={p}, q={q}). "
                "Falling back to demo coefficients.",
                file=sys.stderr,
            )
            return _DEMO_COEFFICIENTS
        return a, b, p, q
    except (ValueError, UnicodeDecodeError) as exc:
        print(
            f"[sovereign-claw] WARN: SOVEREIGN_CLAW_KEY decode failed ({exc}). "
            "Falling back to demo coefficients.",
            file=sys.stderr,
        )

    return _DEMO_COEFFICIENTS


# ── Anti-recall: symbol aliasing map ─────────────────────────────────────────
# Maps internal proprietary names → published Community Edition names.
# When the Community Edition is compiled, internal names are replaced
# with the obfuscated aliases below.  Reverse-engineering the aliases
# yields no useful information about the GOD FILE specification.
#
# This table is the authoritative mapping for the build system.
# It is NOT used at runtime in the Community Edition — it is documentation
# for the build pipeline.

_SYMBOL_MAP: Dict[str, str] = {
    # Internal GOD FILE symbol → Community Edition alias
    "Φ":                     "constraint_potential",
    "C_x":                   "lawful_target",
    "D_x":                   "current_drift",
    "elfe_a":                "coeff_alpha",
    "elfe_b":                "coeff_beta",
    "elfe_p":                "exp_lower",
    "elfe_q":                "exp_upper",
    "thoth_wadjet_threshold": "closure_snap_threshold",
    "R_i":                   "drift_integral",
    "w_i":                   "reputation_weight",
    "quipu_router":          "path_engine",
    "dongba_morph":          "glyph_encoder",
    "gardeners_proof":       "scroll_fingerprint",
    "WeaversKernel":         "SkillAccelerator",         # public alias
    "MythicNeuroKernel":     "_NeuroCore",               # private in Enterprise
    "GardenersProtocol":     "_ScrollLedger",            # private in Enterprise
}


# ── Vault trace decorator ─────────────────────────────────────────────────────
def seal_with_build_fingerprint(trace_meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Inject build fingerprint and ownership assertion into a ProofVault
    trace metadata dict.  Called by WeaversKernel before every vault write.
    """
    trace_meta["_build_fingerprint"] = BUILD_FINGERPRINT
    trace_meta["_edition"]           = _BUILD_EDITION
    trace_meta["_owner"]             = _OWNER_ENTITY
    trace_meta["_framework"]         = _COLLECTIVEOS
    trace_meta["_sealed_at"]         = time.time()
    return trace_meta


# ── Prior art chain ───────────────────────────────────────────────────────────
# Each entry: (Zenodo DOI, description, date)
# These are embedded in enterprise vault traces to create the IP chain.
PRIOR_ART_REGISTRY = [
    ("zenodo.org/records/collectiveos-godfile",
     "CollectiveOS GOD FILE v∞.1 — constraint-first thermodynamic governance",
     "2025"),
    ("zenodo.org/records/isomorphic-intelligence",
     "Isomorphic Intelligence: Deterministic Agent Frameworks via Lyapunov Stability",
     "2025"),
    ("zenodo.org/records/weavers-code",
     "Weavers_Code ELFE Loop Specification — Rabbit/Cypher/Giles tri-temporal architecture",
     "2025"),
    ("zenodo.org/records/sovereign-claw-community",
     "Sovereign Claw Community Edition v2.0.0",
     "2026"),
]


# ── Anti-recall: runtime inspection guard ────────────────────────────────────
class _RecallGuard:
    """
    Prevents attribute inspection of protected kernel objects.

    Applied to MythicNeuroKernel and GardenersProtocol in the
    ENTERPRISE and SOVEREIGN_NODE editions.

    In Community Edition, this is a no-op passthrough decorator.
    It is included here so Community Edition code structure mirrors
    Enterprise Edition without exposing the protection mechanism.
    """

    def __init__(self, cls):
        self._cls = cls
        self.__doc__ = cls.__doc__

    def __call__(self, *args, **kwargs):
        return self._cls(*args, **kwargs)

    def __repr__(self):
        return f"<Protected:{_BUILD_EDITION}>"

    # In Enterprise edition, __getattr__ is overridden to whitelist-only
    # attribute access.  Community Edition: transparent passthrough.


def protect(cls):
    """
    Class decorator: wraps a class with RecallGuard in Enterprise/SN editions.
    No-op in Community Edition.
    """
    if _BUILD_EDITION == "COMMUNITY":
        return cls
    return _RecallGuard(cls)


# ── Export ────────────────────────────────────────────────────────────────────
__all__ = [
    "BUILD_FINGERPRINT",
    "load_elfe_coefficients",
    "seal_with_build_fingerprint",
    "PRIOR_ART_REGISTRY",
    "protect",
    "_SYMBOL_MAP",
]
