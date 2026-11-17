"""Import boundary of the production path: every module the monthly commands load, and the rule that they load only production code.

    tools/py tools/check_imports.py                        # closure of ROOTS with line counts, then the verdict; exit 1 if the rule is broken
    tools/py tools/check_imports.py quant.models.scores    # closure of other roots (listing only)

Static analysis of import statements (AST), following modules of this repository only (the repository root, src/, where the quant package lives, and record/).
Imports inside a function are followed too, except those named in DEFERRED: each is a job of its own that production never calls, listed with its reason.
An old name (lab/_compat.py, ALIASES) is followed to the module it now names. The rule: every module reached is in ALLOWED. Outside the quant
package's production layers that is one thing: the registered model definition (lab.paper.model_def, with the lab package and its old-name table that
loading it runs). The capped weights and the frozen-artefact check production once borrowed from lab.core now live in quant.core (weights, frozen; tied to
the research copies by tools/tests/test_duplicate_parity.py). Frozen code, lab.prospective, all of lab.core and quant's research, validation and reports
packages are not allowed, so production cannot start depending on them without this check (tools/tests/test_check_imports.py) failing.
"""
from __future__ import annotations

import ast
import sys
from collections.abc import Sequence
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOTS = ("quant.features.ciqfill.assemble", "quant.core.release.pointer", "quant.validation.audit_panel", "quant.models.scores", "quant.portfolio.targets",
         "quant.execution.engine", "quant.execution.preflight", "quant.execution.brokers.alpaca.pull_assets", "quant.core.kernels",
         "quant.ops.borrow_watch", "quant.portfolio.shadow", "quant.ops.month", "quant.ops.forward")                # monthly monitors, the cycle, the forward ledger
ALLOWED = ("quant", "quant.core", "quant.core.*", "quant.data", "quant.data.*", "quant.features", "quant.features.*", "quant.models", "quant.models.*",
           "quant.portfolio", "quant.portfolio.*", "quant.execution", "quant.execution.*", "quant.ops", "quant.ops.*",
           "quant.validation", "quant.validation.audit_panel",               # the release audit is a monthly command; the rest of validation is not production
           "lab", "lab._compat", "lab.paper", "lab.paper.model_def")              # the registered definition of the traded model
DEFERRED = {
    "quant.data.free.archive.universe": "the BRIDGE snapshot job's stock universe (the frozen forward test's panel); production only calls latest_day",
}
ALIAS_FILE = "lab/_compat.py"


def aliases(root: Path | None = None) -> dict[str, str]:
    """The old-name table (ALIASES in lab/_compat.py) of the repository at `root`, read from its source; {} if there is none."""
    base = root or REPO
    path = next((p for p in (base / ALIAS_FILE, base / "record" / ALIAS_FILE) if p.is_file()), None)
    if path is None:
        return {}
    for n in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ALIASES" for t in n.targets):
            table: dict[str, str] = ast.literal_eval(n.value)
            return table
    return {}


def new_name(name: str, table: dict[str, str]) -> str | None:
    """The module an old name stands for (the rule of lab._compat.target): its own entry, or the nearest moved package's entry plus the rest; None if none."""
    parts = name.split(".")
    for i in range(len(parts), 0, -1):
        head = ".".join(parts[:i])
        if head in table:
            return ".".join([table[head], *parts[i:]])
    return None


def module_path(mod: str) -> Path | None:
    """The file of repository module `mod` (`a/b.py` or `a/b/__init__.py`, at the repository root, under src/ or under record/), or None if it is not part of this repository."""
    for root in (REPO, REPO / "src", REPO / "record"):
        p = root / Path(*mod.split("."))
        found = next((c for c in (p.with_suffix(".py"), p / "__init__.py") if c.exists()), None)
        if found is not None:
            return found
    return None


def compat_target(tree: ast.Module) -> str | None:
    """The module a forwarding file stands for (the two `config` modules frozen code imports): the string argument of its module-level `moved(__name__, "x")`."""
    for n in tree.body:
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and isinstance(n.value.func, ast.Name) and n.value.func.id == "moved":
            args = n.value.args
            if len(args) >= 2 and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str):
                return args[1].value
    return None


def module_file(mod: str) -> Path:
    """module_path(mod) of a module that must exist; LookupError naming it otherwise (a mistyped root, for example)."""
    path = module_path(mod)
    if path is None:
        raise LookupError(f"{mod} is not a module of this repository ({REPO})")
    return path


def imported_base(pkg: str, node: ast.ImportFrom) -> str:
    """The module a `from ... import` in package `pkg` reads from: the absolute name as written, or the relative one resolved against `pkg`
    (`from . import x` -> `pkg`, `from ..y import x` -> the parent of `pkg` plus `.y`)."""
    if not node.level:
        return node.module or ""
    parts = pkg.split(".")[: len(pkg.split(".")) - node.level + 1]
    return ".".join([*parts, *([node.module] if node.module else [])])


def imports(mod: str) -> dict[str, str | None]:
    """{imported repository module: enclosing function qualname or None for module level}."""
    path = module_file(mod)
    pkg = mod if path.name == "__init__.py" else mod.rpartition(".")[0]
    out: dict[str, str | None] = {}
    table = aliases()

    def visit(node: ast.AST, scope: str | None) -> None:
        for n in ast.iter_child_nodes(node):
            inner = f"{mod}.{n.name}" if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and scope is None else scope
            names: list[str] = []
            if isinstance(n, ast.Import):
                names = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                base = imported_base(pkg, n).rstrip(".")
                names = [base] + [f"{base}.{a.name}" for a in n.names]
            for m in [x for name in names for x in (name, new_name(name, table)) if x]:      # an old name loads its real parents and the module it now names
                parts = m.split(".")
                for i in range(1, len(parts) + 1):                      # importing a.b.c also loads a and a.b
                    sub = ".".join(parts[:i])
                    if module_path(sub) and (sub not in out or out[sub] is not None):
                        out[sub] = scope
            visit(n, inner)

    tree = ast.parse(path.read_text(encoding="utf-8"))
    visit(tree, None)
    target = compat_target(tree)
    if target is not None:                                              # a forwarding file loads the module it names
        parts = target.split(".")
        for i in range(1, len(parts) + 1):
            if module_path(".".join(parts[:i])):
                out[".".join(parts[:i])] = None
    out.pop(mod, None)
    return out


def closure(roots: Sequence[str]) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """({module: the module that first imported it}, [(deferred function, module it would import)])."""
    seen: dict[str, str] = {r: "<root>" for r in roots}
    skipped: list[tuple[str, str]] = []
    stack = list(roots)
    while stack:
        m = stack.pop()
        for dep, scope in sorted(imports(m).items(), key=lambda kv: kv[0]):            # keys are unique: the same order as sorting the pairs
            if scope is not None and scope in DEFERRED:
                skipped.append((scope, dep))
            elif dep not in seen:
                seen[dep] = m
                stack.append(dep)
    return seen, skipped


def allowed(mod: str) -> bool:
    """Whether `mod` is in the production set: named in ALLOWED, or under a package named there with `.*`."""
    return any(mod == a or (a.endswith(".*") and mod.startswith(a[:-1])) for a in ALLOWED)


ROOTS = tuple(r for r in ROOTS if module_path(r) is not None)  # type: ignore[assignment]  # the public edition ships a subset of the roots
DEFERRED["quant.portfolio.targets.short_leg"] = "the short basket is not part of the public edition (the example model is long only)"


def main(argv: list[str]) -> int:
    """Print the closure of ROOTS (or of the modules in `argv`) with line counts; 1 if a module outside ALLOWED is reached from ROOTS, else 0."""
    seen, skipped = closure(argv or ROOTS)
    total = 0
    for m in sorted(seen):
        n = len(module_file(m).read_text(encoding="utf-8").splitlines())
        total += n
        print(f"{n:5d}  {'' if allowed(m) else 'NOT ALLOWED  '}{m}  <- {seen[m]}")
    for scope in sorted({s for s, _ in skipped}):
        print(f"       not followed: {scope} -> {', '.join(sorted({d for s, d in skipped if s == scope}))} ({DEFERRED[scope]})")
    bad = sorted(m for m in seen if not allowed(m))
    print(f"{len(seen)} modules, {total} lines; {'rule holds' if not bad else f'{len(bad)} modules outside the production set: {bad}'}")
    return 1 if (bad and not argv) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
