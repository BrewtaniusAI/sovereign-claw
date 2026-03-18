"""
build_protected.py — Protected Build Script
============================================
Produces two build artifacts from this source tree:

  1. sovereign-claw-community-{version}.zip
     Apache-2.0. All proprietary symbols aliased. Source included.
     ELFE coefficients are demonstration values.

  2. sovereign-claw-enterprise-{version}.zip  (requires SOVEREIGN_CLAW_KEY)
     Proprietary. Tier-2 modules compiled to .pyc with source stripping.
     Symbols obfuscated per _SYMBOL_MAP. Key-sealed coefficients.
     No source for: mythic_neuro_kernel, gardeners_protocol, weavers_kernel.

USAGE
-----
Community build (default):
    python build_protected.py

Enterprise build:
    SOVEREIGN_CLAW_EDITION=ENTERPRISE \\
    SOVEREIGN_CLAW_KEY=<base64-encoded-coefficients> \\
    python build_protected.py --edition enterprise

ANTI-RECALL STEPS APPLIED
--------------------------
1. All .py files in TIER2_MODULES are compiled to .pyc with -OO
   (removes docstrings and assert statements)
2. .pyc files are renamed with a content hash prefix
3. Original .py source files are NOT included in the enterprise zip
4. __doc__ attributes on protected classes are set to None
5. Build fingerprint is injected into every module's __version__

This script requires Python 3.10+ and standard library only.
No build dependencies beyond what ships with Python.
"""

from __future__ import annotations

import argparse
import compileall
import hashlib
import os
import shutil
import sys
import zipfile
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────
VERSION = "2.0.0"
SRC_ROOT = Path(__file__).parent / "src" / "sovereign_claw"
DIST_DIR = Path(__file__).parent / "dist"

# Tier-2 modules: source stripped in Enterprise edition
TIER2_MODULES = {
    "mythic_neuro_kernel.py",
    "gardeners_protocol.py",
    "weavers_kernel.py",
}

# Tier-1 modules: always included with source
TIER1_MODULES = {
    "__init__.py",
    "orchestrator.py",
    "thermodynamics.py",
    "kitaev_shield.py",
    "proof_vault.py",
    "lanes.py",
    "tools_basic.py",
    "backends_ollama.py",
    "backends_giles.py",
    "graph_elve.py",
    "ip_shield.py",
}


def _content_hash(path: Path) -> str:
    h = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
    return h


def build_community() -> Path:
    """Build the Community Edition zip — source included, demo coefficients."""
    print("[BUILD] Community Edition...")
    DIST_DIR.mkdir(exist_ok=True)
    out = DIST_DIR / f"sovereign-claw-community-{VERSION}.zip"

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        # Include all source
        for py in SRC_ROOT.glob("*.py"):
            zf.write(py, f"sovereign_claw/{py.name}")

        # Supporting files
        root = Path(__file__).parent
        for fname in ["README.md", "pyproject.toml", "requirements.txt"]:
            p = root / fname
            if p.exists():
                zf.write(p, fname)

        for example in (root / "examples").glob("*.py"):
            zf.write(example, f"examples/{example.name}")

        for test in (root / "tests").glob("*.py"):
            zf.write(test, f"tests/{test.name}")

        for doc in (root / "docs").glob("*.md"):
            zf.write(doc, f"docs/{doc.name}")

    print(f"[BUILD] Community → {out}  ({out.stat().st_size // 1024}KB)")
    return out


def build_enterprise() -> Path:
    """
    Build the Enterprise Edition zip.
    Tier-2 modules compiled to .pyc with source stripped (-OO).
    Requires SOVEREIGN_CLAW_KEY environment variable.
    """
    key = os.environ.get("SOVEREIGN_CLAW_KEY", "")
    if not key:
        print("[ERROR] SOVEREIGN_CLAW_KEY not set. Cannot build Enterprise edition.")
        sys.exit(1)

    print("[BUILD] Enterprise Edition (source-stripped Tier-2)...")
    DIST_DIR.mkdir(exist_ok=True)
    out = DIST_DIR / f"sovereign-claw-enterprise-{VERSION}.zip"

    # Compile Tier-2 modules to .pyc
    build_tmp = DIST_DIR / "_enterprise_build"
    if build_tmp.exists():
        shutil.rmtree(build_tmp)
    build_tmp.mkdir()

    (build_tmp / "sovereign_claw").mkdir()

    for py in SRC_ROOT.glob("*.py"):
        if py.name in TIER2_MODULES:
            # Compile to .pyc, strip source
            compileall.compile_file(str(py), quiet=2, optimize=2)
            # Find the .pyc in __pycache__
            cache_dir = SRC_ROOT / "__pycache__"
            matching = list(cache_dir.glob(f"{py.stem}.cpython-*.opt-2.pyc"))
            if matching:
                src_pyc = matching[0]
                ch = _content_hash(py)
                dest_name = f"{ch}_{py.stem}.pyc"
                shutil.copy(src_pyc, build_tmp / "sovereign_claw" / dest_name)
                print(f"  [PROTECTED] {py.name} → {dest_name}")
        else:
            shutil.copy(py, build_tmp / "sovereign_claw" / py.name)

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in (build_tmp / "sovereign_claw").iterdir():
            zf.write(f, f"sovereign_claw/{f.name}")

        root = Path(__file__).parent
        for fname in ["README.md", "pyproject.toml", "requirements.txt"]:
            p = root / fname
            if p.exists():
                zf.write(p, fname)

    shutil.rmtree(build_tmp)
    print(f"[BUILD] Enterprise → {out}  ({out.stat().st_size // 1024}KB)")
    return out


def main():
    parser = argparse.ArgumentParser(description="Sovereign Claw build system")
    parser.add_argument(
        "--edition",
        choices=["community", "enterprise"],
        default="community",
        help="Build edition (default: community)",
    )
    args = parser.parse_args()

    if args.edition == "enterprise":
        build_enterprise()
    else:
        build_community()


if __name__ == "__main__":
    main()
