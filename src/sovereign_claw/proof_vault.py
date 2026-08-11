"""
proof_vault.py — ProofVault public compatibility surface
========================================================

The issue #15 authority implementation lives in ``proof_vault_v2``.  This
module deliberately stays as the stable import surface used by the runtime,
existing integrations, and tests.
"""

from .proof_vault_v2 import *  # noqa: F403
from .proof_vault_v2 import __all__ as __all__
