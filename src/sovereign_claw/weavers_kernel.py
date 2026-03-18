# (ONLY showing the fixed __init__ section — everything else stays EXACTLY the same)

class WeaversKernel:
    """
    Human-in-the-loop skill leveling orchestrator.
    """

    def __init__(
        self,
        vault: Optional[ProofVault] = None,
        gardeners_db: Optional[Path] = None,
        neuro_config: Optional[Dict[str, Any]] = None,
        coach_weight: float = 0.6,
    ) -> None:
        self._neuro = MythicNeuroKernel(**(neuro_config or {}))

        # ✅ FIX: avoid unsafe __defaults__ indexing (mypy-safe)
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