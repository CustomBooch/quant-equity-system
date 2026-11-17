"""Where the read-only layer's reports go and how they are written: data/raw_ext/t212_reports/ (git-ignored: account data), a JSON file and its Markdown summary
under one stem. A report is a record: neither file is written if either exists, and nothing is overwritten. JSON is written with sorted keys and a fixed
layout, so the same inputs give the same bytes."""
from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from pathlib import Path

from quant.core.paths import REPO

REPORTS = REPO / "data/raw_ext/t212_reports"


def dumps(payload: Mapping[str, object]) -> str:
    """The report's JSON text: sorted keys, one-space indent, non-ASCII kept, a final newline."""
    return json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def stamp(t: dt.datetime) -> str:
    """A UTC time as a file-name stamp, YYYYMMDDTHHMMSSZ. Refuses a naive time."""
    if t.utcoffset() is None:
        raise ValueError("a report stamp needs a timezone-aware time")
    return t.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def write(stem: str, payload: Mapping[str, object], markdown: str, out_dir: Path = REPORTS) -> tuple[Path, Path]:
    """Write <stem>.json and <stem>.md in `out_dir`; FileExistsError, before anything is written, if either exists."""
    json_path, md_path = out_dir / f"{stem}.json", out_dir / f"{stem}.md"
    if existing := [str(p) for p in (json_path, md_path) if p.exists()]:
        raise FileExistsError(f"{', '.join(existing)} exist(s): reports are never overwritten")
    out_dir.mkdir(parents=True, exist_ok=True)
    with json_path.open("x", encoding="utf-8") as f:
        f.write(dumps(payload))
    with md_path.open("x", encoding="utf-8") as f:
        f.write(markdown if markdown.endswith("\n") else markdown + "\n")
    return json_path, md_path
