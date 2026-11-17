"""The registry of data sources: `@register` on a DataSource subclass makes it known to the store, the command line and the contract tests.

Plugins live in quant.data.sources.plugins, one module per source; `discover()` imports every module there, so adding a file is enough.
"""
from __future__ import annotations

import importlib
import pkgutil

from quant.data.sources.base import DataSource

_SOURCES: dict[str, type[DataSource]] = {}
PLUGINS = "quant.data.sources.plugins"


def register(cls: type[DataSource]) -> type[DataSource]:
    """Class decorator: add a source under its dataset name. A second source with the same name, or an unsound declaration, raises ValueError."""
    ds = cls.dataset
    problems = ds.problems()
    if problems:
        raise ValueError(f"{cls.__name__}: {'; '.join(problems)}")
    if ds.name in _SOURCES and _SOURCES[ds.name] is not cls:
        raise ValueError(f"{ds.name} is already registered by {_SOURCES[ds.name].__name__}")
    _SOURCES[ds.name] = cls
    return cls


def discover(package: str = PLUGINS) -> list[str]:
    """Import every module of the plugin package (each registers its sources); the module names imported."""
    pkg = importlib.import_module(package)
    names = [f"{package}.{m.name}" for m in pkgutil.iter_modules(pkg.__path__) if not m.name.startswith("_") and m.name != "tests"]
    for n in names:
        importlib.import_module(n)
    return names


def sources() -> dict[str, type[DataSource]]:
    """{dataset name: source class} of every registered source, after discovery."""
    discover()
    return dict(sorted(_SOURCES.items()))


def get(name: str) -> DataSource:
    """An instance of the source registered as `name`; KeyError listing the known names otherwise."""
    known = sources()
    if name not in known:
        raise KeyError(f"no data source {name!r}; known: {', '.join(known) or 'none'}")
    return known[name]()
