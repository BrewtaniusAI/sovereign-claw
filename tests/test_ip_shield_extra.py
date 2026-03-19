from __future__ import annotations

import base64
import os

from sovereign_claw import ip_shield


def test_load_coefficients_default_community():
    os.environ.pop("SOVEREIGN_CLAW_EDITION", None)
    os.environ.pop("SOVEREIGN_CLAW_KEY", None)

    coeffs = ip_shield.load_elfe_coefficients()

    assert coeffs == (1.0, 1.0, 0.5, 2.0)


def test_load_coefficients_enterprise_missing_key():
    os.environ["SOVEREIGN_CLAW_EDITION"] = "ENTERPRISE"
    os.environ.pop("SOVEREIGN_CLAW_KEY", None)

    coeffs = ip_shield.load_elfe_coefficients()

    assert coeffs == (1.0, 1.0, 0.5, 2.0)


def test_load_coefficients_valid_key():
    os.environ["SOVEREIGN_CLAW_EDITION"] = "ENTERPRISE"

    raw = "2.0:3.0:0.5:2.5"
    encoded = base64.b64encode(raw.encode()).decode()
    os.environ["SOVEREIGN_CLAW_KEY"] = encoded

    coeffs = ip_shield.load_elfe_coefficients()

    assert coeffs == (2.0, 3.0, 0.5, 2.5)


def test_load_coefficients_invalid_format_fallback():
    os.environ["SOVEREIGN_CLAW_EDITION"] = "ENTERPRISE"
    os.environ["SOVEREIGN_CLAW_KEY"] = base64.b64encode(b"bad:data").decode()

    coeffs = ip_shield.load_elfe_coefficients()

    assert coeffs == (1.0, 1.0, 0.5, 2.0)


def test_load_coefficients_invalid_values_fallback():
    os.environ["SOVEREIGN_CLAW_EDITION"] = "ENTERPRISE"

    raw = "1.0:1.0:1.5:2.0"
    encoded = base64.b64encode(raw.encode()).decode()
    os.environ["SOVEREIGN_CLAW_KEY"] = encoded

    coeffs = ip_shield.load_elfe_coefficients()

    assert coeffs == (1.0, 1.0, 0.5, 2.0)


def test_seal_with_build_fingerprint_adds_fields():
    meta = {}

    sealed = ip_shield.seal_with_build_fingerprint(meta)

    assert "_build_fingerprint" in sealed
    assert "_edition" in sealed
    assert "_owner" in sealed
    assert "_framework" in sealed
    assert "_sealed_at" in sealed


def test_protect_noop_in_community():
    class Dummy:
        pass

    wrapped = ip_shield.protect(Dummy)

    assert wrapped is Dummy


def test_symbol_map_contains_expected_keys():
    assert "Φ" in ip_shield._SYMBOL_MAP
    assert ip_shield._SYMBOL_MAP["WeaversKernel"] == "SkillAccelerator"
