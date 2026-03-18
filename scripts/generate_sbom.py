from __future__ import annotations

import json
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
OUT_DIR = ROOT / "sbom"
OUT_FILE = OUT_DIR / "sovereign-claw.cdx.json"

data = tomllib.loads(PYPROJECT.read_text())
project = data["project"]

components = []
for dep in project.get("dependencies", []):
    name = dep.split(">=")[0].split("==")[0].split("[")[0].strip()
    version = dep.replace(name, "", 1).strip() or "unspecified"
    components.append({
        "type": "library",
        "name": name,
        "version": version.lstrip("=<>!~ "),
        "purl": f"pkg:pypi/{name}",
        "scope": "required",
    })

for extra, deps in project.get("optional-dependencies", {}).items():
    for dep in deps:
        name = dep.split(">=")[0].split("==")[0].split("[")[0].strip()
        version = dep.replace(name, "", 1).strip() or "unspecified"
        components.append({
            "type": "library",
            "name": name,
            "version": version.lstrip("=<>!~ "),
            "purl": f"pkg:pypi/{name}",
            "scope": f"optional:{extra}",
        })

sbom = {
    "bomFormat": "CycloneDX",
    "specVersion": "1.5",
    "serialNumber": f"urn:uuid:sovereign-claw-{project['version']}",
    "version": 1,
    "metadata": {
        "component": {
            "type": "application",
            "name": project["name"],
            "version": project["version"],
            "description": project.get("description", ""),
            "licenses": [{"license": {"id": "Apache-2.0"}}],
        }
    },
    "components": components,
}

OUT_DIR.mkdir(exist_ok=True)
OUT_FILE.write_text(json.dumps(sbom, indent=2) + "\n")
print(f"Wrote {OUT_FILE}")
