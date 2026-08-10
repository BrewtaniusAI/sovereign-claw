from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(autouse=True)
def _allow_insecure_encryptor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allow the dev-only SimpleEncryptor for all tests by setting the opt-in env var."""
    monkeypatch.setenv("SOVEREIGN_SECRETS_ALLOW_INSECURE", "1")
