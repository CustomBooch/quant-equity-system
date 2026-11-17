"""Permno -> Trading 212 instrument mapping: the one implementation used by the target book, the dry-run orders, the preflight and research scripts.

A US ISIN is "US" + the 8-character CUSIP + a check digit, so characters 3-10 of the ISIN are matched with the CRSP CUSIP. Several Trading 212 lines can share an ISIN (EUR or GBX
lines of the same stock); the USD `_US_EQ` line then wins. Fallback: the `_US_EQ` line whose short name is the CRSP ticker. Read-only: local files only, no network.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path

import duckdb
import pandas as pd

from quant.core.paths import REPO, WORK_DB

USD_SUFFIX = "_US_EQ"


def instruments_file(day: str = "*") -> Path:
    """Where quant.execution.brokers.t212.pull_instruments stores the instrument list pulled on `day` (YYYY-MM-DD); "*" matches every day."""
    return REPO / f"data/raw_ext/t212_{day}/instruments.json"


def latest_instruments_file() -> Path | None:
    f = sorted(glob.glob(str(instruments_file())))
    return Path(f[-1]) if f else None


def latest_instruments() -> pd.DataFrame:
    """The newest instrument list pulled by quant.execution.brokers.t212.pull_instruments."""
    f = latest_instruments_file()
    if f is None:
        raise FileNotFoundError("no Trading 212 instrument list; run python -m quant.execution.brokers.t212.pull_instruments")
    return pd.DataFrame(json.loads(f.read_text(encoding="utf-8")))


def instrument_index(inst: pd.DataFrame) -> tuple[dict[str, dict], dict[str, dict]]:
    """(by CUSIP-8 from US ISINs, USD line preferred; by short name, USD lines only) over the stock instruments."""
    stock = inst[inst["type"] == "STOCK"]
    us_first = stock.assign(_us=stock["ticker"].astype(str).str.endswith(USD_SUFFIX)).sort_values("_us")      # later rows overwrite earlier ones: USD lines last
    by_isin = {i[2:10]: r for i, r in zip(us_first["isin"], us_first.to_dict("records"), strict=True) if str(i).startswith("US")}
    by_name = {r["shortName"]: r for r in stock.to_dict("records") if str(r["ticker"]).endswith(USD_SUFFIX)}
    return by_isin, by_name


def crsp_ids(permnos) -> pd.DataFrame:
    """Latest CUSIP and ticker per permno from the CRSP daily file in the engine work database (index permno)."""
    ids = ",".join(str(int(p)) for p in permnos)
    if not ids:
        return pd.DataFrame(columns=["cusip", "ticker"], index=pd.Index([], name="permno"))
    with duckdb.connect(str(WORK_DB), read_only=True) as c:
        return c.execute(f"SELECT permno, arg_max(cusip, date) AS cusip, arg_max(ticker, date) AS ticker FROM crsp_daily WHERE permno IN ({ids}) GROUP BY permno").df().set_index("permno")


def map_ids(ids: pd.DataFrame, inst: pd.DataFrame) -> pd.DataFrame:
    """`ids` has columns cusip and ticker. Adds instrument (best Trading 212 line, any currency), isin, name, ccy, max_qty and t212 (the instrument if it is a USD line, else None)."""
    by_isin, by_name = instrument_index(inst)
    hits = [by_isin.get(str(cu)[:8]) or by_name.get(str(tk)) for cu, tk in zip(ids["cusip"], ids["ticker"], strict=True)]
    out = ids.copy()
    out["instrument"] = [h["ticker"] if h else None for h in hits]
    out["isin"] = [h["isin"] if h else None for h in hits]
    out["name"] = [h["name"] if h else None for h in hits]
    out["ccy"] = [h["currencyCode"] if h else None for h in hits]
    out["max_qty"] = [h.get("maxOpenQuantity") if h else None for h in hits]
    out["t212"] = [t if t and str(t).endswith(USD_SUFFIX) else None for t in out["instrument"]]
    return out


def map_permnos(permnos, inst: pd.DataFrame | None = None) -> pd.DataFrame:
    """CRSP identifiers of `permnos` mapped to Trading 212 lines (index permno), against the newest instrument list unless `inst` is given."""
    return map_ids(crsp_ids(permnos), latest_instruments() if inst is None else inst)
