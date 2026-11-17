"""Layer rule of the repository (the wiki page Architecture): every module has a layer, and imports point one way only.

    core (0) <- data (1) <- features (2) <- models (3) <- portfolio (4) <- execution (5) <- ops (6)
    research, validation, reports: may import every layer; never imported by layers 0-6

    tools/py tools/check_layout.py                   # the verdict; exit 1 if a rule is broken
    tools/py tools/check_layout.py --graph FILE      # also write the import graph ({module: {file, layer, imports}}) as JSON

Static analysis of import statements (AST; imports inside functions and under TYPE_CHECKING count, relative imports are resolved), over every Python
file git tracks or would track. A module of the quant package (src/quant/<layer>/...) has the layer it sits in. The code that stays under lab/ (frozen,
registered, or the research library lab/core) and panel/ has the layer LEGACY gives its longest listed prefix. An import of an old name (lab/_compat.py,
ALIASES) is an import of the module it now names. Tests, conftest.py and tools/ may import anything and are imported by nothing else.

The rules, each a line of output when broken:
  1. every module has a layer (a new top-level folder needs a LEGACY entry);
  2. a module of layer N (0-6) imports only layers 0..N, so production never imports research, validation or reports;
  3. no module outside the tests imports a test module;
  4. code under src/, tests/ and tools/ imports the new names, never an old one (code under lab/ keeps the names it was written with, and so does a
     file under src/ whose bytes are registered: .gitattributes marks it -text);
  5. no import cycle among non-test modules;
  6. EXCEPTIONS is exact: an edge that breaks rule 2 must be listed there with its reason, and a listed edge that no longer occurs must be removed (the
     list only shrinks);
  7. no file sits at an old name: ALIASES is looked up first, so the file would never be loaded.
tools/check_imports.py keeps its own job: the closure of the production commands against an allowlist.
"""
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_imports import aliases, compat_target, imported_base, new_name  # noqa: E402 - the sibling guard, run as a script

REPO = Path(__file__).resolve().parents[1]
ORDER = {"core": 0, "data": 1, "features": 2, "models": 3, "portfolio": 4, "execution": 5, "ops": 6, "research": 9, "validation": 9, "reports": 9}
TEST = "test"
NEW_CODE = ("src/", "tests/", "tools/")

# Layers of the modules that stay outside src/quant: frozen code, registered code and the research library lab/core, none of which is ever edited. The
# longest listed prefix of a module's name wins; `lab` is the default for the rest of lab/.
LEGACY = {
    "lab._compat": "core", "lab.core.c5_audit": "core", "lab.core.metrics": "core",
    "panel": "data", "lab.data": "data",
    "lab.feat_price": "features", "lab.prospective.inputs": "features",
    "lab.core.analyst_pipeline": "features", "lab.core.analystdet_features": "features", "lab.core.newfeat_features": "features",
    "lab.core.nset_features": "features",
    "lab.paper.model_def": "models",
    "lab.portfolio.policy": "portfolio", "lab.core.econ": "portfolio",
    "lab": "research", "legacy_research": "research",
    "archive": "research", "models": "research", "docs": "research",          # the archive, the frozen candidate l3o1, the owner's audit scripts
    "conftest": TEST, "tools": TEST, "rust_core": TEST,
}

# Edges that break rule 2 today. Each is inside frozen or registered code, or is a recorded design decision.
EXCEPTIONS = {
    ("quant.portfolio.plan", "quant.execution.audit"): "a plan is recorded in the audit log when it is written (lab/paper/SPEC.md, amendment A5)",
}


class Module:
    """One Python file: where it is, its imports of other repository modules, the old names it imports them by, and the module it stands for if it is a
    forwarding file. (A plain class: tools/tests load this script without registering it in sys.modules, which a dataclass needs.)"""
    __slots__ = ("file", "imports", "old_names", "target")

    def __init__(self, file: str, imports: set[str], old_names: set[str], target: str | None) -> None:
        self.file, self.imports, self.old_names, self.target = file, imports, old_names, target


def module_name(rel: str) -> str | None:
    """`src/quant/core/x.py` -> quant.core.x, `record/lab/a/__init__.py` -> lab.a; None if a folder in the path cannot be part of a module name (the signal
    repository, signal-repository, is its own project)."""
    parts = rel[:-3].split("/")
    if parts[0] in ("src", "record"):
        parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts and all(p.isidentifier() for p in parts) else None


def python_files(root: Path) -> list[str]:
    """Every .py file git tracks or would track (ignored files aside), relative to `root`; every .py file below `root` outside a git repository."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py"], cwd=root, capture_output=True, text=True,
                             check=True).stdout
        return sorted({f for f in out.splitlines() if (root / f).is_file()})
    except (OSError, subprocess.CalledProcessError):
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def registered(root: Path = REPO) -> set[str]:
    """Python files whose bytes are registered (.gitattributes marks them -text or eol=crlf): never edited, so they keep the names they were written with."""
    path = root / ".gitattributes"
    if not path.is_file():
        return set()
    rows = [line.split() for line in path.read_text(encoding="utf-8").splitlines()]
    return {r[0] for r in rows if len(r) > 1 and r[0].endswith(".py") and ({"-text", "eol=crlf"} & set(r[1:]))}


def scan(root: Path = REPO) -> dict[str, Module]:
    """{module name: Module} for the repository at `root`; an import names the longest repository module it reaches, an old name the module it now names."""
    files = {m: f for f in python_files(root) if (m := module_name(f))}
    table = aliases(root)
    old: set[str] = set()

    def resolve(name: str) -> str | None:
        new = new_name(name, table)
        if new is not None:
            parts = name.split(".")
            old.add(next(h for i in range(len(parts), 0, -1) if (h := ".".join(parts[:i])) in table))      # the old module or package, not the attribute read from it
        parts = (new or name).split(".")
        return next((m for i in range(len(parts), 0, -1) if (m := ".".join(parts[:i])) in files), None)

    graph: dict[str, Module] = {}
    for mod, rel in files.items():
        with warnings.catch_warnings():                                  # an invalid escape in the scanned code is that code's warning, not the check's
            warnings.simplefilter("ignore")
            tree = ast.parse((root / rel).read_text(encoding="utf-8"), filename=rel)
        pkg = mod if rel.endswith("__init__.py") else mod.rpartition(".")[0]
        found: set[str | None] = set()
        old.clear()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                found |= {resolve(a.name) for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                base = imported_base(pkg, n).rstrip(".")
                found |= {resolve(f"{base}.{a.name}") or resolve(base) for a in n.names}
        target = compat_target(tree)
        used = set(old)
        graph[mod] = Module(rel, {m for m in found if m and m != mod} | ({t} if (target and (t := resolve(target))) else set()), used, target)
    return graph


def is_test(mod: str, info: Module) -> bool:
    parts = info.file.split("/")
    return "tests" in parts[:-1] or parts[-1].startswith("test_") or parts[-1] == "conftest.py" or LEGACY.get(mod.split(".")[0]) == TEST


def layer(mod: str, graph: dict[str, Module]) -> str | None:
    """The layer of `mod`: test, the quant subpackage it is in, the layer of the module a forwarding file names, or LEGACY's longest prefix; None if none applies."""
    info = graph[mod]
    if is_test(mod, info):
        return TEST
    target = info.target if info.target in graph else None
    if target is not None and target != mod:
        return layer(target, graph)
    parts = mod.split(".")
    if parts[0] == "quant":
        return parts[1] if len(parts) > 1 and parts[1] in ORDER else "core"     # quant/__init__.py: the package's own docstring
    return next((LEGACY[p] for i in range(len(parts), 0, -1) if (p := ".".join(parts[:i])) in LEGACY), None)


def cycles(graph: dict[str, Module], nodes: set[str]) -> list[list[str]]:
    """Import cycles among `nodes` (strongly connected components of more than one module), iteratively (Tarjan)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    for start in sorted(nodes):
        if start in index:
            continue
        work = [(start, iter(sorted(graph[start].imports & nodes)))]
        index[start] = low[start] = len(index)
        stack.append(start)
        on.add(start)
        while work:
            v, it = work[-1]
            w = next(it, None)
            if w is None:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        x = stack.pop()
                        on.discard(x)
                        comp.append(x)
                        if x == v:
                            break
                    if len(comp) > 1:
                        out.append(sorted(comp))
            elif w not in index:
                index[w] = low[w] = len(index)
                stack.append(w)
                on.add(w)
                work.append((w, iter(sorted(graph[w].imports & nodes))))
            elif w in on:
                low[v] = min(low[v], index[w])
    return sorted(out)


def findings(graph: dict[str, Module], exceptions: dict[tuple[str, str], str] | None = None, root: Path = REPO) -> list[str]:
    """Every broken rule, one line each (empty when the layout holds)."""
    exceptions = EXCEPTIONS if exceptions is None else exceptions
    table, fixed = aliases(root), registered(root)
    layers = {m: layer(m, graph) for m in graph}
    out = [f"no layer: {m} ({graph[m].file}); add its prefix to LEGACY or move it into src/quant/<layer>" for m in sorted(graph) if layers[m] is None]
    broken: set[tuple[str, str]] = set()
    for m in sorted(graph):
        lm, info = layers[m], graph[m]
        for dep in sorted(info.imports):
            ld = layers[dep]
            if lm is None or ld is None:
                continue
            if ld == TEST and lm != TEST:
                out.append(f"imports a test module: {m} [{lm}] -> {dep}")
            elif lm != TEST and ORDER[lm] < 9 and ORDER[ld] > ORDER[lm]:
                broken.add((m, dep))
                if (m, dep) not in exceptions:
                    out.append(f"against the layer rule: {m} [{lm}] -> {dep} [{ld}]")
        if info.file.startswith(NEW_CODE) and info.file not in fixed:
            out += [f"imports an old name: {m} ({info.file}) -> {o}; import {new_name(o, table)}" for o in sorted(info.old_names)]
    out += [f"a file sits at an old name and is never loaded: {graph[m].file}; {m} is {table[m]} (lab/_compat.py)" for m in sorted(set(graph) & set(table))]
    out += [f"stale exception (the edge no longer exists or no longer breaks the rule; remove it): {a} -> {b}" for a, b in sorted(set(exceptions) - broken)]
    out += [f"import cycle: {' <-> '.join(c)}" for c in cycles(graph, {m for m in graph if layers[m] not in (TEST, None)})]
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--graph", type=Path, help="write the import graph with each module's layer to this JSON file")
    a = ap.parse_args(argv)
    graph = scan()
    if a.graph:
        doc = {m: {"file": i.file, "layer": layer(m, graph), "imports": sorted(i.imports)} for m, i in sorted(graph.items())}
        a.graph.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    problems = findings(graph)
    for p in problems:
        print(p)
    edges = sum(len(i.imports) for i in graph.values())
    print(f"{len(graph)} modules, {edges} imports, {len(EXCEPTIONS)} recorded exceptions; "
          f"{'layout holds' if not problems else f'{len(problems)} problem(s)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
