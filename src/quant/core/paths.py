"""The two roots of this repository, and the paths more than one live module reads.

    CHECKOUT    the git checkout: code and documents (src, tools, tests, docs, r, rust_core, contracts, config)
    RECORD      CHECKOUT/record: the record and the data: lab, panel, models, legacy_research, data (RECORD_FOLDERS)

Every path written in a register, a ledger, a plan, a release pointer or a specification is relative to RECORD (`lab/paper/SPEC.md`,
`data/master/...`), and the frozen code finds its own root as the folder that holds `lab`. So the record folders moved under `record/` together,
and no frozen or registered file and no recorded path changed.

`REPO` is the name registered code imports. It is RECORD, with one addition: joining a name that is not a record folder gives the checkout's
(`REPO / "r/inputs"` is CHECKOUT/r/inputs), as it did when both were one folder. So `REPO / <recorded path>` finds the file,
and `path.relative_to(REPO)` of a record or data file gives the name that is recorded. New code names the root it means: RECORD or CHECKOUT.
"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path, PurePath

if sys.platform == "win32":
    from pathlib import WindowsPath as _ConcretePath
else:
    from pathlib import PosixPath as _ConcretePath

CHECKOUT = Path(__file__).resolve().parents[3]                      # src/quant/core/paths.py -> the checkout
RECORD = CHECKOUT / "record"
RECORD_FOLDERS = ("lab", "panel", "models", "legacy_research", "data", "archive")


def locate(path: str | os.PathLike[str]) -> Path:
    """Where a path written relative to the old single root is now: under RECORD if its first part is a record folder, else under CHECKOUT."""
    p = PurePath(path)
    if p.is_absolute():
        return Path(p)
    return (RECORD if p.parts and p.parts[0] in RECORD_FOLDERS or not p.parts else CHECKOUT) / p


def recorded_name(path: str | os.PathLike[str], root: Path | None = None) -> str:
    """The name a path is recorded and shown under: relative to `root` (RECORD by default); a file of the checkout (docs, r) relative to the checkout."""
    p, base = Path(path), Path(root if root is not None else RECORD)
    if p.is_relative_to(base) or PurePath(base) != PurePath(RECORD):
        return p.relative_to(base).as_posix()
    return p.relative_to(CHECKOUT).as_posix()


def argument(value: str, cwd: Path | None = None) -> str:
    """A command-line value as the command should read it. A relative path that starts with a record folder and is not found from the working
    folder is a recorded name (`lab/paper/plans/<hash>.json` typed at the checkout): its full path. Anything else is returned as typed."""
    if value.startswith("-") or not ("/" in value or "\\" in value):
        return value
    p = PurePath(value)
    if p.is_absolute() or not p.parts or p.parts[0] not in RECORD_FOLDERS or (Path(cwd or Path.cwd()) / p.parts[0]).exists():
        return value
    return str(locate(p))


class RecordRoot(_ConcretePath):
    """RECORD as a path whose joins and globs go through `locate`. Any path derived from it is an ordinary path."""
    __slots__ = ()

    def _is_root(self) -> bool:
        return PurePath(self) == PurePath(RECORD)

    def __truediv__(self, key: str | os.PathLike[str]) -> Path:           # type: ignore[override]  # an ordinary Path, not another RecordRoot
        return locate(key) if self._is_root() else Path(str(self), key)

    def joinpath(self, *other: str | os.PathLike[str]) -> Path:           # type: ignore[override]
        return locate(PurePath(*other)) if self._is_root() and other else Path(str(self), *other)

    def glob(self, pattern: str, **kwargs: bool) -> Iterator[Path]:         # type: ignore[override]
        first = PurePath(pattern).parts[:1]
        base = CHECKOUT if self._is_root() and first and first[0] not in RECORD_FOLDERS and (CHECKOUT / first[0]).exists() else Path(str(self))
        return base.glob(pattern, **kwargs)


REPO: Path = RecordRoot(RECORD)
WORK_DB = REPO / "data/derived/builds/ext_2026-10-14e/build/work.duckdb"    # engine work database; its crsp_daily table gives the latest CUSIP and ticker of each permno
YAHOO_DAILY = REPO / "data/raw/Yahoo/free_2026/yahoo"                 # Yahoo daily bars, one chart file <permno>.json.gz per stock (frozen: ends 2026-10-01)


def latest_yahoo_bars(parent: Path | None = None) -> Path | None:
    """The newest finished monthly bars folder, `yahoo_YYYY-MM-DD` with its MANIFEST.json (RUNBOOK step 2), in `parent` (default: beside YAHOO_DAILY); None if none."""
    found = sorted(d for d in (YAHOO_DAILY.parent if parent is None else parent).glob("yahoo_????-??-??") if (d / "MANIFEST.json").exists())
    return found[-1] if found else None
