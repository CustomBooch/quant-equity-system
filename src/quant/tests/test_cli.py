"""quant.cli: the one entry point names real modules, runs the named one with its arguments, and refuses a name it does not know."""
from __future__ import annotations

import pytest

from quant import cli as CLI
from quant.core.paths import CHECKOUT, RECORD


def test_every_command_names_a_module_that_exists():
    assert CLI.missing() == []


def test_without_a_command_the_list_is_printed_and_nothing_runs(capsys):
    assert CLI.main([]) == 0
    out = capsys.readouterr().out
    assert all(f"  {name} " in out for name in CLI.COMMANDS) and "usage: tools/py -m quant <command>" in out


def test_an_unknown_command_exits_2_and_names_it(capsys):
    assert CLI.main(["sned"]) == 2
    err = capsys.readouterr().err
    assert "unknown command 'sned'" in err and "engine" in err


def test_a_command_runs_its_module_as_main_with_the_remaining_arguments(tmp_path, monkeypatch, capsys):
    (tmp_path / "fake_tool.py").write_text("import sys\nif __name__ == '__main__':\n    print('ran', sys.argv[1:])\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(CLI.COMMANDS, "fake", CLI.Command("fake_tool", "a test command"))
    monkeypatch.setattr("sys.argv", ["quant"])
    assert CLI.main(["fake", "--month", "2026-11-01"]) == 0
    assert capsys.readouterr().out == "ran ['--month', '2026-11-01']\n"


def test_a_recorded_name_typed_at_the_checkout_reaches_the_command_as_the_file_under_record(tmp_path, monkeypatch, capsys):
    (tmp_path / "fake_tool.py").write_text("import sys\nif __name__ == '__main__':\n    print(sys.argv[1])\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(CLI.COMMANDS, "fake", CLI.Command("fake_tool", "a test command"))
    monkeypatch.setattr("sys.argv", ["quant"])
    monkeypatch.chdir(CHECKOUT)
    assert CLI.main(["fake", "lab/paper/SPEC.md"]) == 0
    assert capsys.readouterr().out.strip() == str(RECORD / "lab/paper/SPEC.md")


def test_a_commands_own_exit_code_is_passed_on(tmp_path, monkeypatch):
    (tmp_path / "failing_tool.py").write_text("import sys\nif __name__ == '__main__':\n    sys.exit(3)\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(CLI.COMMANDS, "failing", CLI.Command("failing_tool", "a test command"))
    monkeypatch.setattr("sys.argv", ["quant"])
    with pytest.raises(SystemExit) as e:
        CLI.main(["failing"])
    assert e.value.code == 3
