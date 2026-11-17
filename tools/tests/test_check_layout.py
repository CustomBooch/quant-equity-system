"""tools/check_layout.py: the repository holds the layer rule, and each rule catches a planted module that breaks it."""
from __future__ import annotations

import json
import subprocess

import pytest
import tools_loader

CL = tools_loader.load("check_layout")
BASE = {"lab/__init__.py": "", "lab/_compat.py": "ALIASES = {'lab.paths': 'quant.core.paths', 'lab.extension.compare': 'quant.validation.compare'}\n",
        "src/quant/__init__.py": "",
        **{f"src/quant/{layer}/__init__.py": "" for layer in CL.ORDER}}


@pytest.fixture
def repo(tmp_path):
    """plant(files) writes files into a fresh git repository (left untracked: the check reads what git would track) and returns its findings."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)

    def plant(files: dict[str, str], exceptions: dict[tuple[str, str], str] | None = None) -> list[str]:
        for rel, src in {**BASE, **files}.items():
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src, encoding="utf-8")
        return CL.findings(CL.scan(tmp_path), exceptions or {}, tmp_path)
    return plant


def test_the_repository_holds_the_layer_rule_with_exactly_its_recorded_exceptions():
    assert CL.findings(CL.scan()) == []


def test_production_importing_research_validation_or_reports_is_caught(repo):
    out = repo({"src/quant/research/study.py": "", "src/quant/reports/table.py": "",
                "src/quant/portfolio/book.py": "from quant.research import study\n",
                "src/quant/execution/send.py": "def f():\n    import quant.reports.table\n"})
    assert out == ["against the layer rule: quant.execution.send [execution] -> quant.reports.table [reports]",
                   "against the layer rule: quant.portfolio.book [portfolio] -> quant.research.study [research]"]


def test_a_layer_may_import_lower_layers_but_not_higher_ones(repo):
    out = repo({"src/quant/data/bars.py": "from quant.features import mom\n", "src/quant/features/mom.py": "from quant.data import calendar\n",
                "src/quant/data/calendar.py": "from quant.core import paths\n", "src/quant/core/paths.py": ""})
    assert out == ["against the layer rule: quant.data.bars [data] -> quant.features.mom [features]"]


def test_research_validation_and_reports_may_import_every_layer(repo):
    assert repo({"src/quant/execution/send.py": "", "src/quant/research/study.py": "from quant.execution import send\nfrom quant.reports import t\n",
                 "src/quant/reports/t.py": "from quant.validation import v\n", "src/quant/validation/v.py": "from quant.research import study as s\n"}) == [
        "import cycle: quant.reports.t <-> quant.research.study <-> quant.validation.v"]


def test_an_import_by_an_old_name_is_an_import_of_the_module_it_now_names(repo):
    out = repo({"src/quant/validation/compare.py": "", "lab/paper/__init__.py": "", "lab/paper/model_def.py": "from lab.extension import compare\n"})
    assert out == ["against the layer rule: lab.paper.model_def [models] -> quant.validation.compare [validation]"]


def test_new_code_must_import_the_new_name_while_code_under_lab_and_registered_files_keep_the_old_one(repo):
    out = repo({"src/quant/core/paths.py": "", ".gitattributes": "src/quant/research/registered.py -text\n",
                "src/quant/research/study.py": "from lab.paths import REPO\nfrom lab import _compat\n", "lab/run.py": "from lab.paths import REPO\n",
                "src/quant/research/registered.py": "from lab.paths import REPO\n", "tests/test_study.py": "import lab.paths\n"})
    assert out == ["imports an old name: quant.research.study (src/quant/research/study.py) -> lab.paths; import quant.core.paths",
                   "imports an old name: tests.test_study (tests/test_study.py) -> lab.paths; import quant.core.paths"]


def test_a_file_left_at_an_old_name_is_reported_because_it_would_never_be_loaded(repo):
    assert repo({"src/quant/core/paths.py": "", "lab/paths.py": "REPO = 1\n"}) == [
        "a file sits at an old name and is never loaded: lab/paths.py; lab.paths is quant.core.paths (lab/_compat.py)"]


def test_a_module_in_a_folder_without_a_layer_is_reported(repo):
    assert repo({"newtop/__init__.py": "", "newtop/thing.py": ""}) == [
        "no layer: newtop (newtop/__init__.py); add its prefix to LEGACY or move it into src/quant/<layer>",
        "no layer: newtop.thing (newtop/thing.py); add its prefix to LEGACY or move it into src/quant/<layer>"]


def test_a_module_outside_the_tests_importing_a_test_module_is_caught(repo):
    out = repo({"src/quant/features/tests/__init__.py": "", "src/quant/features/tests/factories.py": "",
                "src/quant/features/mom.py": "from quant.features.tests import factories\n",
                "src/quant/features/tests/test_mom.py": "from quant.features.tests import factories\nfrom quant.research import x\n",
                "src/quant/research/x.py": ""})
    assert out == ["imports a test module: quant.features.mom [features] -> quant.features.tests.factories"]


def test_an_import_cycle_is_caught_even_within_one_layer(repo):
    assert repo({"src/quant/core/a.py": "from quant.core import b\n", "src/quant/core/b.py": "def f():\n    from quant.core import a\n"}) == [
        "import cycle: quant.core.a <-> quant.core.b"]


def test_an_unrecorded_break_fails_a_recorded_one_passes_and_a_stale_record_fails(repo):
    files = {"src/quant/core/a.py": "from quant.data import b\n", "src/quant/data/b.py": ""}
    assert repo(files, {("quant.core.a", "quant.data.b"): "frozen"}) == []
    assert repo(files, {("quant.core.a", "quant.data.b"): "frozen", ("quant.core.a", "quant.data.gone"): "frozen"}) == [
        "stale exception (the edge no longer exists or no longer breaks the rule; remove it): quant.core.a -> quant.data.gone"]


def test_relative_imports_and_imports_for_type_checkers_count(repo):
    out = repo({"src/quant/portfolio/book.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from ..research import study\n",
                "src/quant/research/study.py": "", "src/quant/data/x.py": "from . import y\n", "src/quant/data/y.py": "from .. import features\n"})
    assert out == ["against the layer rule: quant.data.y [data] -> quant.features [features]",
                   "against the layer rule: quant.portfolio.book [portfolio] -> quant.research.study [research]"]


def test_a_folder_that_cannot_be_part_of_a_module_name_is_its_own_project_and_skipped(repo):
    assert repo({"signal-repository/src/rank_signals/__init__.py": "", "signal-repository/src/rank_signals/x.py": "import quant.research\n"}) == []


def test_the_graph_is_written_with_each_modules_layer(tmp_path):
    out = tmp_path / "graph.json"
    assert CL.main(["--graph", str(out)]) == 0
    g = json.loads(out.read_text(encoding="utf-8"))
    assert g["quant.core.chain"]["layer"] == "core" and "lab.paths" not in g and "quant.core.paths" in g["quant.models.scores"]["imports"]
    assert g["quant.execution.engine"]["layer"] == "execution" and g["quant.models.definition"]["layer"] == "models" and g["tools.check_layout"]["layer"] == "test"
