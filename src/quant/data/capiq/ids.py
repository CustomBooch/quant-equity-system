"""Match Capital IQ companies to panel stocks: CUSIP8 first, ticker as the fallback, the largest market equity wins a tie (the shared-class case).

On the June 2026 file this matches 99.3% of companies with a market cap (95.2% by CUSIP alone, 98.6% by ticker alone). Capital IQ is company level while the panel is security
level, so multi-class companies (Alphabet, Berkshire) can differ in market equity; the match keeps the class with the largest market equity.
"""
from __future__ import annotations

import pandas as pd


def cusip8(cusip: pd.Series) -> pd.Series:
    """The issuer and issue part of a CUSIP as every source is matched on it: the first eight characters, upper case, as a nullable string."""
    return cusip.astype("string").str[:8].str.upper()


def ticker_norm(ticker: pd.Series) -> pd.Series:
    """A ticker as it is matched across sources: upper case, dots removed (BRK.B and BRKB are one ticker), as a nullable string."""
    return ticker.astype("string").str.upper().str.replace(".", "", regex=False)


def match_permno(t: pd.DataFrame, state: pd.DataFrame) -> pd.DataFrame:
    """`t`: a CiqMonth table. `state`: columns permno, cusip, ticker, me. Returns t with `permno` (nullable Int64) and `match_how` ('cusip', 'ticker' or NA)."""
    s = state.copy()
    s["c8"] = cusip8(s["cusip"])
    s["tk"] = ticker_norm(s["ticker"])
    s = s.sort_values("me", ascending=False)
    by_cusip = s.dropna(subset=["c8"]).drop_duplicates("c8").set_index("c8")["permno"]
    by_tick = s.dropna(subset=["tk"]).drop_duplicates("tk").set_index("tk")["permno"]
    out = t.copy()
    a = out["cusip8"].map(by_cusip)
    b = out["ticker_norm"].map(by_tick)
    out["permno"] = a.fillna(b).astype("Int64")
    out["match_how"] = pd.Series(pd.NA, index=out.index, dtype="string")
    out.loc[a.notna(), "match_how"] = "cusip"
    out.loc[a.isna() & b.notna(), "match_how"] = "ticker"
    return out
