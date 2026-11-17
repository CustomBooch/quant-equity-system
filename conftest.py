"""Repository-wide pytest configuration.

Missing *environment prerequisites* are reported as SKIPPED with the exact reason, not as failures or collection errors, so they cannot hide (or imitate) real failures:

  * an optional heavy dependency that is not installed (torch, cvxpy, ...), at import or at test time;
  * a raw data file under data/raw/ that is not on this machine (the licensed files are archived; restore them to run the data-gated tests);
  * other untracked data: a file a test declares with the `require_data` fixture (master releases, the panel build, the frozen panel and stored predictions), or
    any file inside the repository that git does not track and whose absence made a test fail (tests that cannot declare it, such as those of the protected
    models/l3o1 tree): skipped where the data is absent (a fresh checkout, CI), a failure on the machine that holds the data (QUANT_DATA_MACHINE=1, set by
    checks.ps1), so a green run there cannot hide lost data.

Anything else stays a failure. Set QUANT_STRICT=1 to turn the conversion off (then every missing prerequisite fails).
"""
from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from pluggy import Result

OPTIONAL_DEPENDENCIES = {"torch", "cvxpy", "transformers", "tensorflow", "xgboost", "pysentiment2"}
STRICT = os.environ.get("QUANT_STRICT") == "1"
DATA_MACHINE = os.environ.get("QUANT_DATA_MACHINE") == "1"
MISSING_MODULE = re.compile(r"No module named '([\w.]+)'")
MISSING_FILE = re.compile(r"""(?:No such file or directory|cannot find the (?:file|path) specified|No files found that match the pattern)[^\n]*?['"]([^'"\n]+)['"]""")
RAW = re.compile(r"data[\\/]+raw[\\/]")
REPO = Path(__file__).resolve().parent


def _untracked_in_repo(path: str) -> bool:
    """Whether `path` lies inside this repository and git does not track it (it is data, not code)."""
    try:
        rel = Path(path.replace("\\\\", "\\")).resolve().relative_to(REPO)
    except (ValueError, OSError):
        return False
    return subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel.as_posix()], cwd=REPO, capture_output=True, check=False).returncode != 0


def _reason(text: str) -> str | None:
    """Why a failure report `text` is a missing environment prerequisite (the skip reason), or None if it is a real failure."""
    m = MISSING_MODULE.search(text)
    if m and m.group(1).split(".")[0] in OPTIONAL_DEPENDENCIES:
        return f"optional dependency '{m.group(1)}' is not installed"
    m = MISSING_FILE.search(text)
    if m and RAW.search(m.group(1)):
        return f"raw data file not on this machine: {m.group(1)}"                  # archived on the data machine too (the wiki page Home, skips)
    if m and not DATA_MACHINE and _untracked_in_repo(m.group(1)):
        return f"data not on this machine: {m.group(1)}"
    return None


@pytest.fixture(scope="session")
def require_data() -> Callable[..., None]:
    """require_data(*paths): skip the test unless every path exists; on the data machine (QUANT_DATA_MACHINE=1) or with QUANT_STRICT=1 a missing path fails it."""
    def check(*paths: Path) -> None:
        missing = [str(p) for p in paths if not Path(p).exists()]
        if missing:
            (pytest.fail if DATA_MACHINE or STRICT else pytest.skip)(f"data not on this machine: {missing[0]}" + (f" and {len(missing) - 1} more" if len(missing) > 1 else ""))
    return check


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector: pytest.Collector) -> Generator[None, Result[pytest.CollectReport], None]:
    """A module that fails to import for a missing prerequisite is reported as skipped with the reason."""
    outcome = yield
    report = outcome.get_result()
    if STRICT or report.outcome != "failed" or not isinstance(collector, pytest.Module):
        return
    why = _reason(str(report.longrepr))
    if why:
        report.outcome = "skipped"
        report.longrepr = (str(collector.path), 0, f"Skipped: {why}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, Result[pytest.TestReport], None]:     # `call` is part of the hook's signature
    """A test whose setup or call fails for a missing prerequisite is reported as skipped with the reason."""
    outcome = yield
    report = outcome.get_result()
    if STRICT or report.when not in ("setup", "call") or report.outcome != "failed":
        return
    why = _reason(str(report.longrepr))
    if why:
        report.outcome = "skipped"
        report.longrepr = (str(item.path), item.reportinfo()[1] or 0, f"Skipped: {why}")
