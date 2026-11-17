"""CRSP identifiers of a stock: its latest CUSIP and ticker, from the CRSP daily table of the engine work database. Read-only, local files only.

The broker mapping (quant.data.instruments.alpaca) starts from these: the latest CRSP ticker of a permno is its Alpaca symbol when that symbol is tradable.
"""
from __future__ import annotations

import duckdb
import pandas as pd

from quant.core.paths import WORK_DB


def crsp_ids(permnos) -> pd.DataFrame:
    """Latest CUSIP and ticker per permno from the CRSP daily file in the engine work database (index permno)."""
    ids = ",".join(str(int(p)) for p in permnos)
    if not ids:
        return pd.DataFrame(columns=["cusip", "ticker"], index=pd.Index([], name="permno"))
    with duckdb.connect(str(WORK_DB), read_only=True) as c:
        return c.execute(f"SELECT permno, arg_max(cusip, date) AS cusip, arg_max(ticker, date) AS ticker FROM crsp_daily WHERE permno IN ({ids}) "
                         "GROUP BY permno").df().set_index("permno")
