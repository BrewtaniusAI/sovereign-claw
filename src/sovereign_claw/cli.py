"""
cli.py — Sovereign Claw Command Center
=======================================

Governed execution CLI with:
- human-readable output by default
- raw JSON output via --json
- provider selection with safe fallback
- side-effect-free preview mode via --preview
- proof receipt export via --emit-receipt
- policy profile selection via --policy-profile
- budget-aware execution via --budget
- trace inspection, replay, drift breakdown
- provider stats, policy testing, memory stats
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from typing import Any, Dict

from .orchestrator import Orchestrator
from .runtime import SovereignRuntime

from . import __version__
from .config import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_FILE,
    _dataclass_to_dict,
    init_config_dir,
    load_config,
)
from .skills import SkillsManager


class DemoBackend:
    """
    DEVELOPMENT ONLY — Minimal stub backend for CLI smoke testing.

    WARNING: This backend is strictly for local development and testing.
    It does NOT connect to any real LLM provider. Do NOT use in production.
    All actions are deterministic echo operations with no real inference.

    For production usage, configure a real provider:
      sovereign run "task" --provider ollama
      sovereign run "task" --provider giles
    """

    def decide_next_action(
        self,
        objective: str,
        history: list[Dict[str, Any]],
        forbidden_actions: list[str],
        drift: float,
    ) -> Dict[str, Any]:
        if drift <= 0.0:
            return {
                "tool": "HALT",
                "kwargs": {},
                "comment": "already closed",
                "agent_id": "demo_backend",
            }

        return {
            "tool": "echo_text",
            "kwargs": {"text": f"objective={objective}"},
            "comment": "[DEV-ONLY] safe demo action — not a real provider response",
            "agent_id": "demo_backend",
        }


def echo_text(text: str) -> str:
    return text


def pretty_print_result(result: dict) -> None:  # type: ignore[type-arg]
    mode = "Preview" if result.get("preview") else "Execution"
    print(f"\n=== Sovereign {mode} ===\n")

    if "trace_id" in result and result["trace_id"]:
        print(f"Trace ID: {result['trace_id']}")

    print(f"Status: {result['status']}")

    if "reason" in result and result["reason"]:
        print(f"Reason: {result['reason']}")

    if "steps" in result and result["steps"] is not None:
        print(f"Steps: {result['steps']}")

    if "final_drift" in result and result["final_drift"] is not None:
        try:
            print(f"Final Drift: {float(result['final_drift']):.4f}")
        except (TypeError, ValueError):
            print(f"Final Drift: {result['final_drift']}")

    if "provider" in result and result["provider"]:
        print(f"Provider: {result['provider']}")

    if "policy_status" in result and result["policy_status"]:
        print(f"Policy: {result['policy_status']}")

    if "action" in result:
        print("\nProposed Action:")
        print(json.dumps(result["action"], indent=2))

    if "drift_trajectory" in result and result["drift_trajectory"]:
        print("\nDrift Trajectory:")
        for i, drift_val in enumerate(result["drift_trajectory"]):
            try:
                print(f"  {i}: {float(drift_val):.4f}")
            except (TypeError, ValueError):
                print(f"  {i}: {drift_val}")

    print("\n===========================\n")


def build_runtime(provider: str = "demo") -> SovereignRuntime:
    """
    Build a SovereignRuntime with the specified provider backend.

    Note: The 'demo' provider uses DemoBackend, which is strictly for
    local development and CLI smoke testing. It performs no real inference.
    For production usage, specify 'ollama', 'giles', or a configured provider.
    """
    backend: Any = None

    if provider == "ollama":
        try:
            from .backends_ollama import RabbitOllama

            backend = RabbitOllama()
        except Exception:
            print(
                "[WARN] Ollama backend unavailable, falling back to demo "
                "(DEVELOPMENT ONLY — not for production)",
                file=sys.stderr,
            )
            backend = DemoBackend()

    elif provider == "giles":
        try:
            from .backends_giles import GilesTiered, GilesTieredConfig, ProviderConfig

            backend = GilesTiered(
                GilesTieredConfig(
                    primary=ProviderConfig(
                        name="openai",
                        api_key="DUMMY_KEY",
                        model="gpt-4o-mini",
                    )
                )
            )
        except Exception:
            print(
                "[WARN] Giles backend unavailable, falling back to demo "
                "(DEVELOPMENT ONLY — not for production)",
                file=sys.stderr,
            )
            backend = DemoBackend()

    else:
        backend = DemoBackend()

    orchestrator = Orchestrator(
        llm_backend=backend,
        tools={"echo_text": echo_text},
    )
    return SovereignRuntime(orchestrator=orchestrator)


# ── Subcommand handlers ──────────────────────────────────────────────────────
def _cmd_onboard(args: argparse.Namespace) -> int:
    """Bootstrap configuration directory and install bundled skills."""
    config_dir = init_config_dir()
    print(f"Config directory: {config_dir}")

    cfg = load_config()
    mgr = SkillsManager(skills_dirs=[str(d) for d in cfg.skills_dirs])
    installed = mgr.install_bundled()
    for name in installed:
        mgr.evaluate(name)
        mgr.activate(name)
    print(f"Installed {len(installed)} bundled skills: {', '.join(installed)}")
    print("\nOnboarding complete. Run 'sovereign doctor' to verify.")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Run system health diagnostics."""
    checks = []

    # Config directory
    exists = DEFAULT_CONFIG_DIR.exists()
    checks.append(("Config directory", exists, str(DEFAULT_CONFIG_DIR)))

    # Config file
    exists = DEFAULT_CONFIG_FILE.exists()
    checks.append(("Config file", exists, str(DEFAULT_CONFIG_FILE)))

    # Python version
    py_ok = sys.version_info >= (3, 10)
    checks.append(("Python >= 3.10", py_ok, f"{sys.version_info.major}.{sys.version_info.minor}"))

    # httpx available
    httpx_available = importlib.util.find_spec("httpx") is not None
    checks.append(
        (
            "httpx installed",
            httpx_available,
            "" if httpx_available else "pip install httpx",
        )
    )

    # pydantic available
    pydantic_available = importlib.util.find_spec("pydantic") is not None
    checks.append(
        (
            "pydantic installed",
            pydantic_available,
            "" if pydantic_available else "pip install pydantic",
        )
    )

    print("\n=== Sovereign Doctor ===\n")
    all_ok = True
    for label, ok, note in checks:
        status = "OK" if ok else "FAIL"
        if not ok:
            all_ok = False
        line = f"  [{status}] {label}"
        if note:
            line += f"  ({note})"
        print(line)
    print()

    if all_ok:
        print("All checks passed.")
    else:
        print("Some checks failed. Fix the issues above and re-run.")
    return 0 if all_ok else 1


def _cmd_gateway(args: argparse.Namespace) -> int:
    """Show gateway status."""
    cfg = load_config()
    print("\n=== Gateway Status ===\n")
    print(f"  Host:            {cfg.gateway.host}")
    print(f"  Port:            {cfg.gateway.port}")
    print(f"  Max connections: {cfg.gateway.max_connections}")
    print(f"  Heartbeat:       {cfg.gateway.heartbeat_interval}s")
    print(f"  Session timeout: {cfg.gateway.session_timeout}s")
    print()
    return 0


def _cmd_skills(args: argparse.Namespace) -> int:
    """List installed skills."""
    mgr = SkillsManager()
    mgr.install_bundled()
    for name in list(mgr._bundled_skills.keys()):
        mgr.evaluate(name)
        mgr.activate(name)

    skills = mgr.list_skills()
    if args.json_output:
        print(json.dumps([s.spec.to_dict() for s in skills], indent=2))
    else:
        print(f"\n=== Skills ({len(skills)}) ===\n")
        for s in skills:
            status = s.status.value
            tools = ", ".join(s.spec.tools_provided) if s.spec.tools_provided else "none"
            print(f"  [{status:>9}] {s.spec.name} v{s.spec.version}")
            print(f"             {s.spec.description}")
            print(f"             tools: {tools}")
            print()
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    """View current configuration."""
    cfg = load_config()
    data = _dataclass_to_dict(cfg)
    if args.json_output:
        print(json.dumps(data, indent=2, default=str))
    else:
        print("\n=== Configuration ===\n")
        for key, val in data.items():
            if isinstance(val, dict):
                print(f"  {key}:")
                for k2, v2 in val.items():
                    print(f"    {k2}: {v2}")
            elif isinstance(val, list) and val and isinstance(val[0], dict):
                print(f"  {key}: [{len(val)} items]")
            else:
                print(f"  {key}: {val}")
        print()
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    """Run a governed objective."""
    runtime = build_runtime(args.provider)

    if args.preview:
        result = runtime.preview(
            args.objective,
            forbidden_actions=args.forbid,
            t_max_steps=args.t_max_steps,
            risk_threshold=args.risk_threshold,
        )
    else:
        result = runtime.run(
            args.objective,
            forbidden_actions=args.forbid,
            t_max_steps=args.t_max_steps,
            risk_threshold=args.risk_threshold,
        )

    # Emit receipt if requested
    if getattr(args, "emit_receipt", False) and result.get("trace_id"):
        from .proof_vault import ProofVault
        from .receipts import ReceiptBuilder

        vault = ProofVault()
        builder = ReceiptBuilder(vault)
        receipt_output = builder.export(result["trace_id"], fmt="json")
        print("\n=== Proof Receipt ===")
        print(receipt_output)
        print("=====================\n")

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        pretty_print_result(result)
    return 0


def _cmd_version(args: argparse.Namespace) -> int:
    print(f"sovereign-claw v{__version__}")
    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    """Inspect an execution trace."""
    from .proof_vault import ProofVault

    vault = ProofVault()
    summary = vault.get_trace_summary(args.trace_id)
    if args.json_output:
        print(json.dumps(summary, indent=2, default=str))
    else:
        print(f"\n=== Trace: {args.trace_id} ===\n")
        for key, val in summary.items():
            print(f"  {key}: {val}")
        print()
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    """Replay an execution step-by-step."""
    from .proof_vault import ProofVault
    from .receipts import ReceiptBuilder

    vault = ProofVault()
    builder = ReceiptBuilder(vault)
    steps = builder.replay(args.trace_id)

    if args.json_output:
        import dataclasses

        print(json.dumps([dataclasses.asdict(s) for s in steps], indent=2))
    else:
        print(f"\n=== Replay: {args.trace_id} ({len(steps)} steps) ===\n")
        for step in steps:
            drift_dir = "+" if step.drift_delta >= 0 else ""
            print(
                f"  [{step.step_index}] {step.action} "
                f"drift={step.drift:.4f} ({drift_dir}{step.drift_delta:.4f}) "
                f"status={step.status}"
            )
            if step.comment:
                print(f"         {step.comment}")
        print()
    return 0


def _cmd_drift(args: argparse.Namespace) -> int:
    """Show drift breakdown for a trace."""
    from .proof_vault import ProofVault

    vault = ProofVault()
    steps = vault.get_trace_steps(args.trace_id)

    if args.json_output:
        print(
            json.dumps(
                {
                    "trace_id": args.trace_id,
                    "steps": len(steps),
                    "drift_trajectory": [s.drift for s in steps],
                },
                indent=2,
            )
        )
    else:
        print(f"\n=== Drift: {args.trace_id} ({len(steps)} steps) ===\n")
        prev_drift = 1.0
        for step in steps:
            delta = step.drift - prev_drift
            direction = "+" if delta >= 0 else ""
            print(
                f"  [{step.step_index}] drift={step.drift:.4f} "
                f"({direction}{delta:.4f}) {step.action}"
            )
            prev_drift = step.drift
        print()
    return 0


def _cmd_providers(args: argparse.Namespace) -> int:
    """List providers with stats."""
    cfg = load_config()
    providers = cfg.providers

    if args.json_output:
        data = [_dataclass_to_dict(p) for p in providers]
        print(json.dumps(data, indent=2, default=str))
    else:
        print(f"\n=== Providers ({len(providers)}) ===\n")
        for p in providers:
            configured = p.is_configured()
            status = "configured" if configured else "not configured"
            print(f"  [{status:>14}] {p.name}")
            print(f"                  model: {p.model}")
            print(f"                  priority: {p.priority}")
            print()

        # Demo backend warning
        print("  [  dev-only   ] demo")
        print("                  DEVELOPMENT ONLY — not for production\n")
    return 0


def _cmd_policy_test(args: argparse.Namespace) -> int:
    """Test policy against sample input."""
    from .policy_engine import PolicyEngine, PolicyProfile

    profile = PolicyProfile(args.profile) if args.profile else PolicyProfile.BALANCED
    engine = PolicyEngine(profile=profile)

    sample = {"tool": args.tool, "trace_id": args.trace_id or ""}
    if args.drift is not None:
        engine.update_drift(args.drift)

    result = engine.test_policy(sample)

    if args.json_output:
        print(
            json.dumps(
                {
                    "allowed": result.allowed,
                    "reasons": result.reasons,
                    "matched_policies": result.matched_policies,
                    "profile": result.profile,
                    "drift_at_evaluation": result.drift_at_evaluation,
                },
                indent=2,
            )
        )
    else:
        status = "ALLOWED" if result.allowed else "DENIED"
        print(f"\n=== Policy Test ({profile.value}) ===\n")
        print(f"  Tool:    {args.tool}")
        print(f"  Result:  {status}")
        if result.reasons:
            print("  Reasons:")
            for r in result.reasons:
                print(f"    - {r}")
        if result.matched_policies:
            print("  Matched:")
            for m in result.matched_policies:
                print(f"    - {m}")
        print()
    return 0


def _cmd_memory(args: argparse.Namespace) -> int:
    """Show memory stats."""
    from .memory import MemoryStore

    store = MemoryStore()
    stats = store.stats()

    if args.json_output:
        import dataclasses

        print(json.dumps(dataclasses.asdict(stats), indent=2))
    else:
        print("\n=== Memory Stats ===\n")
        print(f"  Total entries:  {stats.total_entries}")
        print(f"  Episodic:       {stats.episodic_count}")
        print(f"  Semantic:       {stats.semantic_count}")
        print(f"  Task:           {stats.task_count}")
        print(f"  Expired:        {stats.expired_count}")
        print(f"  Avg relevance:  {stats.avg_relevance:.2f}")
        print()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sovereign",
        description="Sovereign Claw — governed sovereign agent runtime",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── run ────────────────────────────────────────────────────────────────
    run_parser = subparsers.add_parser("run", help="Run a governed objective")
    run_parser.add_argument("objective", help="Objective to execute")
    run_parser.add_argument(
        "--forbid",
        action="append",
        default=[],
        help="Forbidden tool/action (can be repeated)",
    )
    run_parser.add_argument(
        "--t-max",
        type=int,
        default=8,
        dest="t_max_steps",
        help="Maximum execution steps",
    )
    run_parser.add_argument(
        "--risk-threshold",
        type=float,
        default=0.9,
        help="Soft halt threshold",
    )
    run_parser.add_argument(
        "--provider",
        choices=["demo", "ollama", "giles"],
        default="demo",
        help="Backend provider (demo is DEVELOPMENT ONLY)",
    )
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON instead of formatted view",
    )
    run_parser.add_argument(
        "--preview",
        action="store_true",
        help="Compute governed outcome without execution side effects",
    )
    run_parser.add_argument(
        "--emit-receipt",
        action="store_true",
        dest="emit_receipt",
        help="Output proof receipt after execution",
    )
    run_parser.add_argument(
        "--policy-profile",
        choices=["strict", "balanced", "exploratory"],
        default="balanced",
        dest="policy_profile",
        help="Policy profile for governance strictness",
    )
    run_parser.add_argument(
        "--budget",
        type=float,
        default=None,
        help="Max cost budget for execution (USD)",
    )

    # ── onboard ───────────────────────────────────────────────────────────
    subparsers.add_parser("onboard", help="Bootstrap config and install bundled skills")

    # ── doctor ────────────────────────────────────────────────────────────
    subparsers.add_parser("doctor", help="Run system health diagnostics")

    # ── gateway ───────────────────────────────────────────────────────────
    subparsers.add_parser("gateway", help="Show gateway configuration and status")

    # ── skills ────────────────────────────────────────────────────────────
    skills_parser = subparsers.add_parser("skills", help="List installed skills")
    skills_parser.add_argument("--json", dest="json_output", action="store_true")

    # ── config ────────────────────────────────────────────────────────────
    config_parser = subparsers.add_parser("config", help="View current configuration")
    config_parser.add_argument("--json", dest="json_output", action="store_true")

    # ── version ───────────────────────────────────────────────────────────
    subparsers.add_parser("version", help="Print version and exit")

    # ── trace ─────────────────────────────────────────────────────────────
    trace_parser = subparsers.add_parser("trace", help="Inspect execution trace")
    trace_parser.add_argument("trace_id", help="Trace ID to inspect")
    trace_parser.add_argument("--json", dest="json_output", action="store_true")

    # ── replay ────────────────────────────────────────────────────────────
    replay_parser = subparsers.add_parser("replay", help="Replay execution step-by-step")
    replay_parser.add_argument("trace_id", help="Trace ID to replay")
    replay_parser.add_argument("--json", dest="json_output", action="store_true")

    # ── drift ─────────────────────────────────────────────────────────────
    drift_parser = subparsers.add_parser("drift", help="Show drift breakdown for trace")
    drift_parser.add_argument("trace_id", help="Trace ID to analyze")
    drift_parser.add_argument("--json", dest="json_output", action="store_true")

    # ── providers ─────────────────────────────────────────────────────────
    providers_parser = subparsers.add_parser("providers", help="List providers with stats")
    providers_parser.add_argument("--json", dest="json_output", action="store_true")

    # ── policy ────────────────────────────────────────────────────────────
    policy_parser = subparsers.add_parser("policy", help="Test policy against sample input")
    policy_sub = policy_parser.add_subparsers(dest="policy_command")
    test_parser = policy_sub.add_parser("test", help="Test policy evaluation")
    test_parser.add_argument("--tool", default="echo_text", help="Tool to test")
    test_parser.add_argument("--trace-id", default="", help="Trace ID for test")
    test_parser.add_argument("--drift", type=float, default=None, help="Current drift level")
    test_parser.add_argument(
        "--profile",
        choices=["strict", "balanced", "exploratory"],
        default="balanced",
        help="Policy profile to test under",
    )
    test_parser.add_argument("--json", dest="json_output", action="store_true")

    # ── memory ────────────────────────────────────────────────────────────
    memory_parser = subparsers.add_parser("memory", help="Show memory stats")
    memory_parser.add_argument("--json", dest="json_output", action="store_true")

    args = parser.parse_args(argv)

    dispatch = {
        "run": _cmd_run,
        "onboard": _cmd_onboard,
        "doctor": _cmd_doctor,
        "gateway": _cmd_gateway,
        "skills": _cmd_skills,
        "config": _cmd_config,
        "version": _cmd_version,
        "trace": _cmd_trace,
        "replay": _cmd_replay,
        "drift": _cmd_drift,
        "providers": _cmd_providers,
        "policy": _cmd_policy_test,
        "memory": _cmd_memory,
    }

    handler = dispatch.get(args.command)
    if handler:
        return handler(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
