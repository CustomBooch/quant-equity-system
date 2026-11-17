"""Alpaca instrument list and CRSP -> Alpaca symbol mapping (PAPER amendment A10). The list is pulled read-only by quant.execution.brokers.alpaca.pull_assets; this module reads local files only.

    dotenvx run -- python -m quant.execution.brokers.alpaca.pull_assets     # (read-only) -> data/raw_ext/alpaca_<date>/assets.json

Mapping: the latest CRSP ticker of a permno (quant.data.instruments.t212.crsp_ids) is the Alpaca symbol when that symbol is active and tradable. Otherwise, CRSP keeps the
share class apart from the ticker (BRK, BF, MOG), so the one tradable Alpaca symbol of the form TICKER.X is used when there is exactly one. Else no line."""
from __future__ import annotations

import glob
import json
from pathlib import Path

import pandas as pd

from quant.core.paths import REPO
from quant.data.instruments.t212 import crsp_ids

COLUMNS = ["symbol", "name", "status", "tradable", "fractionable", "shortable", "easy_to_borrow", "exchange"]


def assets_file(day: str = "*") -> Path:
    return REPO / f"data/raw_ext/alpaca_{day}/assets.json"


def latest_assets_file() -> Path | None:
    f = sorted(glob.glob(str(assets_file())))
    return Path(f[-1]) if f else None


def latest_assets() -> pd.DataFrame:
    f = latest_assets_file()
    if f is None:
        raise FileNotFoundError("no Alpaca asset list; run: dotenvx run -- python -m quant.execution.brokers.alpaca.pull_assets")
    return pd.DataFrame(json.loads(f.read_text(encoding="utf-8")))


def list_date() -> str | None:
    """Pull date of the newest asset list (YYYY-MM-DD), or None."""
    f = latest_assets_file()
    return f.parent.name.removeprefix("alpaca_")[:10] if f else None


def tradable_symbols(assets: pd.DataFrame) -> set[str]:
    a = assets[(assets["status"] == "active") & (assets["tradable"].astype(bool))]
    return set(a["symbol"].astype(str))


def line_for(ticker: object, tradable: set[str], by_base: dict[str, list[str]]) -> str | None:
    t = str(ticker).strip().upper() if ticker is not None and ticker == ticker else ""
    if not t:
        return None
    if t in tradable:
        return t
    cands = by_base.get(t, [])
    return cands[0] if len(cands) == 1 else None


def map_ids(ids: pd.DataFrame, assets: pd.DataFrame) -> pd.DataFrame:
    """`ids` has columns cusip and ticker; adds `line`, the Alpaca symbol or None."""
    tradable = tradable_symbols(assets)
    by_base: dict[str, list[str]] = {}
    for s in sorted(tradable):
        if "." in s:
            by_base.setdefault(s.split(".", 1)[0], []).append(s)
    out = ids.copy()
    out["line"] = [line_for(t, tradable, by_base) for t in out["ticker"]]
    return out


def map_permnos(permnos, assets: pd.DataFrame | None = None) -> pd.DataFrame:
    return map_ids(crsp_ids(permnos), latest_assets() if assets is None else assets)


