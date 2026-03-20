import json

from sovereign_claw.cli import main


def test_cli_run_outputs_pretty_view(capsys):
    exit_code = main(["run", "stabilize ai"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Sovereign Execution" in captured.out
    assert "Status:" in captured.out


def test_cli_run_outputs_json_when_requested(capsys):
    exit_code = main(["run", "stabilize ai", "--json"])
    captured = capsys.readouterr()

    assert exit_code == 0

    payload = json.loads(captured.out)
    assert payload["status"] in {"executed", "halted"}


def test_cli_run_accepts_forbidden_actions(capsys):
    exit_code = main(
        ["run", "stabilize ai", "--forbid", "delete_everything", "--t-max", "4", "--json"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0

    payload = json.loads(captured.out)
    assert "status" in payload


def test_cli_help_for_missing_command():
    try:
        main([])
    except SystemExit as exc:
        assert exc.code != 0