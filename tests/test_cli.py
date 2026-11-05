import pytest

from partslib.cli import PLANNED, build_parser, main


def test_cli_exposes_the_commands_in_the_spec():
    subparsers = build_parser()._subparsers._group_actions[0].choices
    assert set(subparsers) == {"check", "build", "install", "add", "scan", "migrate", "verify", "vendor", "serve"}


@pytest.mark.parametrize("command", PLANNED)
def test_unimplemented_commands_fail_rather_than_pretend_to_succeed(command, capsys):
    assert main([command]) == 1
    assert "not implemented" in capsys.readouterr().err


def test_unknown_command_is_rejected():
    with pytest.raises(SystemExit) as excinfo:
        main(["nonsense"])
    assert excinfo.value.code == 2
