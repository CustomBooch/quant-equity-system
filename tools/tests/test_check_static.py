"""tools/check_static.py: the production path has no finding, the reason given for ignoring asserts is true, and planted defects are caught."""
from __future__ import annotations

import ast
import sys

import pytest
import tools_loader

sys.path.insert(0, str(tools_loader.TOOLS))
CS = tools_loader.load("check_static")


def test_the_production_path_has_no_finding():
    assert CS.findings(CS.production_files()) == []


def test_production_modules_contain_no_assert_so_their_checks_hold_under_python_optimise():
    asserts = [f"{f}:{n.lineno}" for f in CS.production_files() for n in ast.walk(ast.parse((CS.CI.REPO / f).read_text(encoding="utf-8"))) if isinstance(n, ast.Assert)]
    assert asserts == []


@pytest.mark.parametrize("code,source", [
    ("B023", "fs = []\nfor i in range(3):\n    fs.append(lambda: i)\n"),                                   # a closure that sees only the last loop value
    ("B905", "pairs = list(zip([1, 2], [3]))\n"),                                                       # a zip that silently drops the unmatched tail
    ("SIM115", "data = open('x').read()\n"),                                                           # a file left open
    ("S110", "try:\n    int('x')\nexcept Exception:\n    pass\n"),                                    # an error swallowed without a reason
    ("PLW1510", "import subprocess\nsubprocess.run(['git', 'status'])\n"),                             # a failed command that goes unnoticed
    ("F821", "print(undefined_name)\n"),
])
def test_a_planted_defect_is_reported_with_its_rule(tmp_path, code, source):
    f = tmp_path / "planted.py"
    f.write_text(source, encoding="utf-8")
    out = CS.findings([str(f)])
    assert len(out) == 1 and f" {code} " in out[0]


def test_a_planted_syntax_error_is_reported(tmp_path):
    f = tmp_path / "planted.py"
    f.write_text("def f(:\n    pass\n", encoding="utf-8")
    assert any(" invalid-syntax: " in x for x in CS.findings([str(f)]))


@pytest.mark.parametrize("line,is_finding", [
    ("lab/paper/engine.py:12:5: F401 [*] `os` imported but unused", True),
    ("C:\\quant\\lab\\paper\\engine.py:12:5: F401 [*] `os` imported but unused", True),        # Windows drive letter
    ("/tmp/pytest-0/planted.py:1:7: F821 Undefined name `x`", True),                           # absolute POSIX path (a file outside the repository)
    ("planted.py:1:8: invalid-syntax: Expected `)`, found newline", True),
    ("Found 2 errors.", False),
    ("[*] 1 fixable with `--fix`.", False),
    ("All checks passed!", False),
])
def test_finding_lines_are_told_from_ruffs_summary_lines_on_every_path_form(line, is_finding):
    assert bool(CS.FINDING.match(line)) is is_finding


def test_a_finding_on_a_line_with_a_reasoned_noqa_is_not_reported(tmp_path):
    f = tmp_path / "planted.py"
    f.write_text("try:\n    int('x')\nexcept Exception:  # noqa: S110 - optional field\n    pass\n", encoding="utf-8")
    assert CS.findings([str(f)]) == []
