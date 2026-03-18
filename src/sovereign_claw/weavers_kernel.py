from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

from sovereign_claw.gardeners_protocol import GardenersProtocol
from sovereign_claw.mythic_neuro_kernel import MythicNeuroKernel
from sovereign_claw.proof_vault import ProofVault


class WeaversKernel:
    def __init__(
        self,
        vault: Optional[ProofVault] = None,
        gardeners_db: Optional[Path] = None,
        neuro_config: Optional[Dict[str, Any]] = None,
        coach_weight: float = 0.6,
    ) -> None:
        self._neuro = MythicNeuroKernel(**(neuro_config or {}))

        if gardeners_db is not None:
            db_path = gardeners_db
        else:
            defaults = GardenersProtocol.__init__.__defaults__
            if defaults and len(defaults) > 0:
                db_path = defaults[0]
            else:
                db_path = None

        self._gardeners = GardenersProtocol(db_path=db_path)
        self._vault = vault or ProofVault()
        self._coach_weight = coach_weight
