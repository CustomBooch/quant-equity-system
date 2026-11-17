"""Exact duplicate functions in the editable code: two functions with the same body (names of variables and arguments ignored, docstrings ignored) of at least
MIN_LINES lines, in src/quant or tools, tests included. A copy is a second place to fix a bug; share it or, where a copy must stay, list it in KNOWN with the reason.

    tools/py tools/check_duplicates.py          # the groups found; exit 1 if any is not in KNOWN
"""
from __future__ import annotations

import ast
import hashlib
import sys
import warnings
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOTS = ("src/quant", "tools")
SKIP = ("tools/public/overlay/",)                     # the public edition's replacement files: each stands in for a private module of the same name
MIN_LINES = 4
KNOWN: dict[frozenset[str], str] = {}


class _Names(ast.NodeTransformer):
    def visit_Name(self, n: ast.Name) -> ast.AST:
        return ast.copy_location(ast.Name(id="_", ctx=n.ctx), n)

    def visit_arg(self, n: ast.arg) -> ast.AST:
        return ast.copy_location(ast.arg(arg="_"), n)


def body_without_docstring(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    b = fn.body
    if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) and isinstance(b[0].value.value, str):
        return b[1:]
    return b


def functions(root: Path = REPO) -> list[tuple[str, str, int, str]]:
    """(file:function, file, lines, structural hash) of every function of at least MIN_LINES lines."""
    out = []
    for top in ROOTS:
        for p in sorted((root / top).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            rel = p.relative_to(root).as_posix()
            if rel.startswith(SKIP):
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")                      # escapes in other files' strings are their own lint's business
                tree = ast.parse(p.read_bytes().decode("utf-8", "replace"))
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.end_lineno is not None and n.end_lineno - n.lineno + 1 >= MIN_LINES:
                    body = body_without_docstring(n)
                    if not body:
                        continue
                    dump = "\n".join(ast.dump(_Names().visit(ast.parse(ast.unparse(s)).body[0])) for s in body)
                    out.append((f"{rel}:{n.name}", rel, n.end_lineno - n.lineno + 1, hashlib.sha256(dump.encode()).hexdigest()))
    return out


def groups(root: Path = REPO) -> list[frozenset[str]]:
    by: dict[str, set[str]] = defaultdict(set)
    for name, _, _, h in functions(root):
        by[h].add(name)
    return [frozenset(g) for g in by.values() if len(g) > 1]


def unknown(root: Path = REPO) -> list[frozenset[str]]:
    return [g for g in groups(root) if g not in KNOWN]


def main() -> int:
    found = groups()
    for g in sorted(found, key=sorted):
        print(("known    " if g in KNOWN else "DUPLICATE") + "  " + "  =  ".join(sorted(g)))
    bad = unknown()
    print(f"{len(found)} group(s) of duplicate functions; {'none unexplained' if not bad else f'{len(bad)} not in KNOWN'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
