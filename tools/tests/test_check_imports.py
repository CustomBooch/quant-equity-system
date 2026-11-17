"""tools/check_imports.py: the production path loads only production code, and the check catches a module that breaks the rule."""
from __future__ import annotations

from pathlib import Path

import pytest
import tools_loader

CI = tools_loader.load("check_imports")


def test_production_loads_only_production_code():
    seen, _ = CI.closure(CI.ROOTS)
    assert sorted(m for m in seen if not CI.allowed(m)) == []
    assert {"lab.run", "lab.data", "lab.prospective", "lab.core.c5_common", "lab.bridge.live_model"}.isdisjoint(seen)


def _tree(root: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")


def test_a_research_import_is_caught_and_a_deferred_one_is_not_followed(tmp_path, monkeypatch):
    _tree(tmp_path, {"lab/__init__.py": "", "lab/run.py": "", "lab/paper/__init__.py": "",
                     "lab/paper/scores.py": "def f():\n    from lab.run import crank\n",
                     "lab/paper/targets.py": "def job():\n    from lab import run\n"})
    monkeypatch.setattr(CI, "REPO", tmp_path)
    monkeypatch.setattr(CI, "DEFERRED", {"lab.paper.targets.job": "test"})
    seen, skipped = CI.closure(["lab.paper.scores"])
    assert "lab.run" in seen and not CI.allowed("lab.run")                        # imports inside functions are followed
    seen, skipped = CI.closure(["lab.paper.targets"])
    assert "lab.run" not in seen and ("lab.paper.targets.job", "lab.run") in skipped


def test_relative_imports_are_resolved_against_the_importing_package(tmp_path, monkeypatch):
    _tree(tmp_path, {"lab/__init__.py": "", "lab/run.py": "", "lab/paper/__init__.py": "", "lab/paper/plan.py": "",
                     "lab/paper/engine.py": "from . import plan\nfrom ..run import x\n"})
    monkeypatch.setattr(CI, "REPO", tmp_path)
    seen, _ = CI.closure(["lab.paper.engine"])
    assert {"lab.paper.plan", "lab.run"} <= set(seen)                            # `from . import plan` used to stop the check with a TypeError


def test_a_root_that_is_not_a_module_of_the_repository_is_refused_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(CI, "REPO", tmp_path)
    with pytest.raises(LookupError, match="lab.nowhere is not a module"):
        CI.closure(["lab.nowhere"])


def test_an_old_name_is_followed_to_the_module_it_now_names_under_src(tmp_path, monkeypatch):
    _tree(tmp_path, {"lab/__init__.py": "", "lab/_compat.py": "ALIASES = {'lab.release': 'quant.core.release'}\n", "lab/paper/__init__.py": "",
                     "lab/paper/model_def.py": "from lab.release import pointer\n",
                     "src/quant/__init__.py": "", "src/quant/core/__init__.py": "", "src/quant/core/release/__init__.py": "",
                     "src/quant/core/release/pointer.py": "import json\n"})
    monkeypatch.setattr(CI, "REPO", tmp_path)
    seen, _ = CI.closure(["lab.paper.model_def"])
    assert set(seen) == {"lab", "lab.paper.model_def", "quant", "quant.core", "quant.core.release", "quant.core.release.pointer"}
    assert all(CI.allowed(m) for m in seen)


def test_production_reaching_quant_research_through_an_old_name_is_caught(tmp_path, monkeypatch):
    _tree(tmp_path, {"lab/__init__.py": "", "lab/_compat.py": "ALIASES = {'lab.bridge.s9': 'quant.research.bridge.s9'}\n", "lab/paper/__init__.py": "",
                     "lab/paper/model_def.py": "from lab.bridge import s9\n",
                     "src/quant/__init__.py": "", "src/quant/research/__init__.py": "", "src/quant/research/bridge/__init__.py": "",
                     "src/quant/research/bridge/s9.py": ""})
    monkeypatch.setattr(CI, "REPO", tmp_path)
    seen, _ = CI.closure(["lab.paper.model_def"])
    assert "quant.research.bridge.s9" in seen and not CI.allowed("quant.research.bridge.s9")


def test_a_repository_without_an_alias_table_has_no_old_names(tmp_path):
    assert CI.aliases(tmp_path) == {} and CI.new_name("lab.paths", {}) is None
