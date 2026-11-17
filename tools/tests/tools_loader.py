"""Load a tools/ script as a module (the scripts are not a package)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

TOOLS = Path(__file__).resolve().parents[1]


def load(name: str) -> ModuleType:
    """tools/<name>.py as a fresh module object; ImportError if it cannot be loaded."""
    spec = importlib.util.spec_from_file_location(name, TOOLS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {TOOLS / f'{name}.py'}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
