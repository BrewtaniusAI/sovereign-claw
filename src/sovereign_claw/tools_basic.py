"""
tools_basic.py — Built-in Tools
================================
A small set of deterministic tools for Orchestrator registration.
These are safe to use in any manifold (no network, no shell).

DRIFT-10 FIX (v2.0.0)
----------------------
The original module exposed plain functions with no metadata, schema, or
safety classification.  The Orchestrator registered them by name only,
meaning malformed kwargs from an LLM caused uncontrolled exceptions rather
than governed drift penalties.

This version adds:
  - ToolSpec dataclass: name, description, required_kwargs, safety_tier
  - TOOL_REGISTRY: authoritative mapping of name → (function, ToolSpec)
  - validate_kwargs(): raises TypeError with a governed message when required
    kwargs are missing, so KitaevZeroMode can translate the error to a
    bounded drift penalty instead of propagating a raw exception.
  - register_all(): convenience helper to register all built-in tools with
    an Orchestrator instance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal


# ── ToolSpec ──────────────────────────────────────────────────────────────────
SafetyTier = Literal["READ_ONLY", "WRITE_LOCAL", "NETWORK", "SHELL"]


@dataclass
class ToolSpec:
    """
    Metadata descriptor for a registered tool.

    Fields
    ------
    name            : Canonical tool name (matches Orchestrator key)
    description     : One-sentence description for LLM context injection
    required_kwargs : List of kwarg names that MUST be present
    safety_tier     : Highest-risk operation the tool performs
    """

    name: str
    description: str
    required_kwargs: List[str] = field(default_factory=list)
    safety_tier: SafetyTier = "READ_ONLY"


def validate_kwargs(spec: ToolSpec, kwargs: Dict[str, Any]) -> None:
    """
    Raise TypeError if any required kwarg is missing.
    The error message is structured so KitaevZeroMode can surface it to the
    LLM as a governed constraint message rather than a raw traceback.
    """
    missing = [k for k in spec.required_kwargs if k not in kwargs]
    if missing:
        raise TypeError(
            f"Tool '{spec.name}' missing required kwargs: {missing}. Recalculate approach vector."
        )


# ── Tool implementations ───────────────────────────────────────────────────────
def echo_text(text: str) -> str:
    """Return text unchanged. Useful for testing closure."""
    validate_kwargs(_SPEC_ECHO, {"text": text})
    return text


def read_text_file(path: str) -> str:
    """Read a local text file and return its contents."""
    validate_kwargs(_SPEC_READ_FILE, {"path": path})
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(f"No such file: {p}")
    return p.read_text(encoding="utf-8")


def write_json_file(path: str, data: Any) -> str:
    """Write data as formatted JSON to path; returns the path string."""
    validate_kwargs(_SPEC_WRITE_JSON, {"path": path, "data": data})
    p = Path(path)
    p.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return str(p)


def list_directory(path: str) -> list[str]:
    """Return a sorted list of file names in directory path."""
    validate_kwargs(_SPEC_LIST_DIR, {"path": path})
    p = Path(path)
    if not p.is_dir():
        raise NotADirectoryError(f"Not a directory: {p}")
    return sorted(str(item.name) for item in p.iterdir())


# ── ToolSpec instances ────────────────────────────────────────────────────────
_SPEC_ECHO = ToolSpec(
    name="echo_text",
    description="Return text unchanged. Useful for testing isomorphic closure.",
    required_kwargs=["text"],
    safety_tier="READ_ONLY",
)

_SPEC_READ_FILE = ToolSpec(
    name="read_text_file",
    description="Read a local text file and return its UTF-8 contents.",
    required_kwargs=["path"],
    safety_tier="READ_ONLY",
)

_SPEC_WRITE_JSON = ToolSpec(
    name="write_json_file",
    description="Write data as formatted JSON to a local file path.",
    required_kwargs=["path", "data"],
    safety_tier="WRITE_LOCAL",
)

_SPEC_LIST_DIR = ToolSpec(
    name="list_directory",
    description="Return a sorted list of file names in a directory.",
    required_kwargs=["path"],
    safety_tier="READ_ONLY",
)


# ── Tool registry ─────────────────────────────────────────────────────────────
#
# TOOL_REGISTRY maps tool name → (callable, ToolSpec).
# Orchestrator.register_tool() accepts only the callable; the ToolSpec is
# available here for schema validation and LLM context injection.
#
TOOL_REGISTRY: Dict[str, tuple[Callable, ToolSpec]] = {
    "echo_text": (echo_text, _SPEC_ECHO),
    "read_text_file": (read_text_file, _SPEC_READ_FILE),
    "write_json_file": (write_json_file, _SPEC_WRITE_JSON),
    "list_directory": (list_directory, _SPEC_LIST_DIR),
}


def register_all(orchestrator: Any) -> None:
    """
    Register all built-in tools with an Orchestrator instance.

    Usage
    -----
        from sovereign_claw import Orchestrator, TaskManifold
        from sovereign_claw.tools_basic import register_all
        orch = Orchestrator(llm_backend=my_llm)
        register_all(orch)
    """
    for name, (fn, _spec) in TOOL_REGISTRY.items():
        orchestrator.register_tool(name, fn)


def tool_descriptions() -> List[Dict[str, Any]]:
    """
    Return a list of tool descriptor dicts suitable for LLM context injection.
    Each dict contains: name, description, required_kwargs, safety_tier.
    """
    return [
        {
            "name": spec.name,
            "description": spec.description,
            "required_kwargs": spec.required_kwargs,
            "safety_tier": spec.safety_tier,
        }
        for _fn, spec in TOOL_REGISTRY.values()
    ]


__all__ = [
    "ToolSpec",
    "SafetyTier",
    "validate_kwargs",
    "echo_text",
    "read_text_file",
    "write_json_file",
    "list_directory",
    "TOOL_REGISTRY",
    "register_all",
    "tool_descriptions",
]
