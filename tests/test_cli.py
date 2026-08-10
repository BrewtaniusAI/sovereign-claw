import json

import pytest

from sovereign_claw.cli import build_runtime, main
from sovereign_claw.policy_engine import PolicyProfile


def test_cli_run_outputs_pretty_view(capsys):
    exit_code = main(["run", "stabilize ai", "--provider", "demo"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Sovereign Execution" in captured.out
    assert "Status:" in captured.out


def test_cli_run_outputs_json_when_requested(capsys):
    exit_code = main(["run", "stabilize ai", "--provider", "demo", "--json"])
    captured = capsys.readouterr()

    assert exit_code == 0

    payload = json.loads(captured.out)
    assert payload["status"] in {"executed", "halted"}
    assert payload["requested_provider"] == "demo"
    assert payload["actual_provider"] == "demo"
    assert payload["budget"]["outcome"] == "not-requested"


def test_cli_run_accepts_forbidden_actions(capsys):
    exit_code = main(
        [
            "run",
            "stabilize ai",
            "--provider",
            "demo",
            "--forbid",
            "delete_everything",
            "--t-max",
            "4",
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0

    payload = json.loads(captured.out)
    assert "status" in payload


def test_cli_preview_uses_native_preview(capsys):
    exit_code = main(["run", "stabilize ai", "--provider", "demo", "--preview", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["status"] == "preview-risk-threshold"
    assert payload["supported"] is True
    assert payload["approvable"] is False
    assert payload["action"]["tool"] == "echo_text"
    assert payload["action_digest"]


def test_cli_run_accepts_expected_action_digest(capsys):
    preview_exit = main(["run", "stabilize ai", "--provider", "demo", "--preview", "--json"])
    preview_payload = json.loads(capsys.readouterr().out)

    exit_code = main(
        [
            "run",
            "stabilize ai",
            "--provider",
            "demo",
            "--expected-action-digest",
            preview_payload["action_digest"],
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert preview_exit == 0
    assert exit_code == 0
    assert payload["status"] in {"executed", "halted"}


@pytest.mark.parametrize(
    "policy_profile",
    [
        PolicyProfile.STRICT,
        PolicyProfile.BALANCED,
        PolicyProfile.EXPLORATORY,
    ],
)
def test_build_runtime_uses_selected_policy_profile(policy_profile):
    runtime, runtime_meta = build_runtime(provider="demo", policy_profile=policy_profile)

    assert runtime.orchestrator.policy_engine.profile is policy_profile
    assert runtime_meta["policy_profile"] == policy_profile.value


def test_build_runtime_rejects_invalid_policy_profile():
    with pytest.raises(ValueError, match="Unsupported policy profile 'invalid'"):
        build_runtime(provider="demo", policy_profile="invalid")


def test_runtime_execution_reports_actual_policy_profile():
    runtime, _ = build_runtime(provider="demo", policy_profile=PolicyProfile.EXPLORATORY)

    payload = runtime.run("stabilize ai", risk_threshold=1.0)

    assert payload["policy_profile"] == "exploratory"
    assert payload["status"] in {"executed", "halted"}


def test_runtime_preview_uses_real_policy_profile_for_governance():
    strict_runtime, _ = build_runtime(provider="demo", policy_profile=PolicyProfile.STRICT)
    balanced_runtime, _ = build_runtime(provider="demo", policy_profile=PolicyProfile.BALANCED)
    exploratory_runtime, _ = build_runtime(provider="demo", policy_profile=PolicyProfile.EXPLORATORY)

    # DemoBackend emits agent_id="demo_backend" while preview starts at drift=1.0,
    # so STRICT's high-drift policy should deny it while BALANCED/EXPLORATORY allow it.
    strict_payload = strict_runtime.preview("stabilize ai", risk_threshold=1.0)
    balanced_payload = balanced_runtime.preview("stabilize ai", risk_threshold=1.0)
    exploratory_payload = exploratory_runtime.preview("stabilize ai", risk_threshold=1.0)

    assert strict_payload["policy_profile"] == "strict"
    assert strict_payload["policy_decision"]["profile"] == "strict"
    assert strict_payload["policy_decision"]["allowed"] is False
    assert strict_payload["status"] == "preview-policy-denied"
    assert strict_payload["expected_halt_reason"]
    assert "trace_id is required by policy" not in strict_payload["expected_halt_reason"]
    assert balanced_payload["policy_profile"] == "balanced"
    assert balanced_payload["policy_decision"]["profile"] == "balanced"
    assert balanced_payload["policy_decision"]["allowed"] is True
    assert balanced_payload["status"] == "preview"
    assert exploratory_payload["policy_profile"] == "exploratory"
    assert exploratory_payload["policy_decision"]["profile"] == "exploratory"
    assert exploratory_payload["policy_decision"]["allowed"] is True
    assert exploratory_payload["status"] == "preview"
    assert strict_payload["status"] != balanced_payload["status"]


def test_cli_budget_is_rejected_with_structured_json(capsys):
    exit_code = main(["run", "stabilize ai", "--provider", "demo", "--budget", "1", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["status"] == "error"
    assert payload["budget"]["outcome"] == "unsupported"


def test_cli_help_for_missing_command():
    try:
        main([])
    except SystemExit as exc:
        assert exc.code != 0
