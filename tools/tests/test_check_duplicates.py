"""tools/check_duplicates.py: the repository has no unexplained duplicate function, and the check finds one that is planted."""
from __future__ import annotations

from pathlib import Path

import tools_loader

CD = tools_loader.load("check_duplicates")


def test_the_repository_has_no_duplicate_function_outside_the_known_list():
    assert CD.unknown() == []


def test_every_known_duplicate_still_exists_so_the_list_does_not_go_stale():
    assert set(CD.KNOWN) <= set(CD.groups())


def tree(root: Path, files: dict[str, str]) -> None:
    for rel, src in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(src)


BODY = "    total = 0\n    for v in values:\n        total += v * 2\n    return total\n"


def test_two_functions_with_the_same_body_under_different_names_are_found_and_a_different_body_is_not(tmp_path):
    tree(tmp_path, {"src/quant/a.py": f'def one(values):\n    """doc."""\n{BODY}',
                    "tools/b.py": "def two(xs):\n    t = 0\n    for x in xs:\n        t += x * 2\n    return t\n",
                    "src/quant/c.py": "def other(values):\n    total = 0\n    for v in values:\n        total += v * 3\n    return total\n"})
    assert CD.groups(tmp_path) == [frozenset({"src/quant/a.py:one", "tools/b.py:two"})]


def test_a_function_shorter_than_the_minimum_is_not_counted(tmp_path):
    tree(tmp_path, {"src/quant/a.py": "def f(x):\n    return x + 1\n", "src/quant/b.py": "def g(y):\n    return y + 1\n", "tools/c.py": ""})
    assert CD.groups(tmp_path) == []
