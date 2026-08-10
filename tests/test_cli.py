import json

from sovereign_claw.cli import main


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
