"""Where pulled data is kept, and how it is read back point in time.

    record/data/sources/<name>/<pull date>/data.parquet       one file per pull (git-ignored, like all data)
    record/data/sources/<name>/<pull date>/MANIFEST.json      the request, rows, date range, the file's sha256, the code commit

A pull never overwrites an earlier one: a revised history arrives as a new pull, and `load` reads the newest pull unless told otherwise.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd

from quant.core.paths import RECORD
from quant.core.release.release import sha256_file
from quant.data.sources.base import DataSource, PullRequest, conform, validate

ROOT = RECORD / "data" / "sources"


class Refused(Exception):
    """The pulled data failed its checks; nothing was stored."""


def pull(source: DataSource, request: PullRequest, root: Path = ROOT, today: dt.date | None = None) -> dict[str, Any]:
    """Fetch, conform, check and store one pull; its manifest. Refused (nothing written) if a check fails or the pull is empty."""
    ds = source.dataset
    day = today or dt.date.today()
    frame = conform(source.fetch(request), ds)
    problems = validate(frame, ds, day)
    if frame.empty:
        problems.append("the pull returned no rows")
    if problems:
        raise Refused(f"{ds.name}: " + "; ".join(problems))
    out = root / ds.name / day.isoformat()
    if (out / "MANIFEST.json").exists():
        raise Refused(f"{ds.name}: already pulled on {day.isoformat()}; a stored pull is never replaced, so nothing was written (move the folder aside to pull again)")
    out.mkdir(parents=True, exist_ok=True)
    f = out / "data.parquet"
    frame.sort_values(ds.key).to_parquet(f, index=False)
    manifest = {
        "dataset": ds.name, "licence": ds.licence, "pulled": day.isoformat(),
        "request": {"start": request.start.isoformat(), "end": request.end.isoformat(), "entities": list(request.entities) if request.entities else None},
        "rows": len(frame), "entities": int(frame[ds.entity].nunique()),
        "observed": [str(frame[ds.observed].min().date()), str(frame[ds.observed].max().date())],
        "known": [str(frame[ds.known].min().date()), str(frame[ds.known].max().date())],
        "file": f.name, "sha256": sha256_file(f),
    }
    (out / "MANIFEST.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return manifest


def pulls(name: str, root: Path = ROOT) -> list[Path]:
    """The pull folders of a dataset, oldest first."""
    d = root / name
    return sorted(p for p in d.iterdir() if (p / "MANIFEST.json").is_file()) if d.is_dir() else []


def load(source: DataSource, as_of: dt.date | None = None, pulled: str | None = None, root: Path = ROOT) -> pd.DataFrame:
    """The stored rows of a dataset (newest pull, or the pull of date `pulled`), checked against the manifest's hash.

    With `as_of`, only rows usable on that date: known (plus the declared lag) on or before it.
    """
    ds = source.dataset
    folders = pulls(ds.name, root)
    if pulled is not None:
        folders = [p for p in folders if p.name == pulled]
    if not folders:
        raise FileNotFoundError(f"no stored pull of {ds.name}" + (f" dated {pulled}" if pulled else "") + f" under {root / ds.name}")
    folder = folders[-1]
    manifest = json.loads((folder / "MANIFEST.json").read_text(encoding="utf-8"))
    f = folder / manifest["file"]
    if sha256_file(f) != manifest["sha256"]:
        raise ValueError(f"{f} does not match the sha256 in its manifest")
    frame = pd.read_parquet(f)
    if as_of is not None:
        frame = frame[usable_from(frame, ds.known, ds.lag_days) <= pd.Timestamp(as_of)]
    return frame.reset_index(drop=True)


def usable_from(frame: pd.DataFrame, known: str, lag_days: int) -> pd.Series:
    """The first date each row may be used: its known date plus the declared lag."""
    return pd.to_datetime(frame[known]) + pd.Timedelta(days=lag_days)
