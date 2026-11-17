"""Capital IQ consensus-estimate files (plain EPS, first forecast year) and the consensus features built from them for bridge months.

Layout of Sheet1 (a screening export): a header row whose first cell is "Entity Name", the mnemonic row under it, then the period row, then the as-of date row (mm/dd/yyyy), then one company per row.
The estimate columns must carry the period FY+1 and one common as-of date. The as-of date is the first of a month and is checked against the estimate dates (none after it, the newest within ten days
of it): that catches a day/month mix-up, which would otherwise shift every estimate by months. Extra columns (the FY0 difference and surprise) are read but not used: they are the last fiscal
year's surprise, not the quarterly surprise the engine uses.

Features (same definitions as panel/pit/ibes.py, from the Capital IQ consensus, so a different vendor's levels):
  numest_fy1 = number of estimates        disp_fy1 = std dev / |mean|        rev_fy1_1m / rev_fy1_3m = (mean - mean one / three months earlier) / |earlier mean|        numest_chg_3m = estimates - estimates three months earlier
Outside a 45-day window (decision date - estimate date) the whole family is empty, as in the engine. `same_day` marks estimates dated on the as-of day itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from quant.data.capiq.ciq import CiqFormatError, cell_date, excel_serial, numeric, screening_sheet, text
from quant.data.capiq.ids import cusip8, match_permno, ticker_norm

ID_MNEM = {"SP_ENTITY_NAME": "name", "SP_ENTITY_ID": "entity_id", "SP_CUSIP": "cusip9", "SP_TICKER": "ticker"}
EST_MNEM = {"SP_EPS_EST": "mean", "SP_EPS_MEDIAN_EST": "med", "SP_EPS_HIGH_EST": "high", "SP_EPS_LOW_EST": "low", "SP_EPS_STDDEV_EST": "std", "SP_EPS_NUM_EST": "num", "SP_EPS_DATE_OF_EST": "estdate"}
PERIOD = "FY+1"
WINDOW_DAYS = 45
EST_COLS = ["numest_fy1", "disp_fy1", "rev_fy1_1m", "rev_fy1_3m", "numest_chg_3m"]


@dataclass
class CiqEst:
    """One consensus export: its as-of date (the first of a month), the company table, the file."""
    asof: pd.Timestamp
    table: pd.DataFrame
    path: str


def load_est(path: str | Path) -> CiqEst:
    """Read and validate one consensus export (module docstring); CiqFormatError if a mnemonic, the FY+1 period or a consistent as-of date is missing,
    or if the estimate dates do not agree with the as-of date."""
    rows, r0 = screening_sheet(path)
    mnem, period, asof = rows.get(r0 + 1, {}), rows.get(r0 + 2, {}), rows.get(r0 + 3, {})
    col_of = {(v or "").strip(): c for c, v in mnem.items() if v}
    problems = [f"missing mnemonic {m}" for m in {**ID_MNEM, **EST_MNEM} if m not in col_of]
    if problems:
        raise CiqFormatError("; ".join(problems))
    bad = [m for m in EST_MNEM if (period.get(col_of[m]) or "").strip() != PERIOD]
    if bad:
        raise CiqFormatError(f"period of {bad} is not {PERIOD}: {[period.get(col_of[m]) for m in bad]}")
    dates = {cell_date(asof.get(col_of[m])) for m in EST_MNEM}
    if len(dates) != 1 or None in dates:
        raise CiqFormatError(f"the estimate columns do not share one as-of date: {sorted(str(d) for d in dates)}")
    a = cast(pd.Timestamp, dates.pop())                                            # not None: checked just above
    if a.day != 1:
        raise CiqFormatError(f"as-of date {a.date()} is not the first of a month")
    recs = []
    for r in sorted(k for k in rows if k >= r0 + 4):
        if (rows[r].get(col_of["SP_ENTITY_NAME"]) or "").strip():
            recs.append({n: rows[r].get(col_of[m]) for m, n in {**ID_MNEM, **EST_MNEM}.items()})
    df = pd.DataFrame(recs)
    for c in ("mean", "med", "high", "low", "std", "num", "estdate"):
        df[c] = numeric(df[c])
    for c in ("cusip9", "ticker", "entity_id"):
        df[c] = text(df[c])
    df["estdate"] = df["estdate"].map(excel_serial).dt.normalize()                # the export carries a time of day on the Excel serial; estimate dates are dates
    ed = df["estdate"].dropna()
    if len(ed) == 0 or ed.max() > a or ed.max() < a - pd.Timedelta(days=10):
        raise CiqFormatError(f"estimate dates (latest {ed.max().date() if len(ed) else None}) do not agree with the as-of date {a.date()}")
    df["cusip8"] = cusip8(df["cusip9"])
    df["ticker_norm"] = ticker_norm(df["ticker"])
    df["age"] = (a - df["estdate"]).dt.days
    df["same_day"] = df["age"] == 0
    return CiqEst(a, df.reset_index(drop=True), str(path))


def _by_permno(est: CiqEst, state: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Match to permnos and keep one row per permno (the most estimates, then the newest estimate). Returns the table and the match statistics among companies with an estimate."""
    t = match_permno(est.table, state)
    has = t["mean"].notna()
    stats = {"with_estimate": int(has.sum()), "match_rate": round(float(t.loc[has, "permno"].notna().mean()), 4) if has.any() else None}
    m = t[t["permno"].notna() & has].sort_values(["num", "estdate"], ascending=[False, False]).drop_duplicates("permno")
    m = m.assign(permno=m["permno"].astype("int64")).set_index("permno")
    return m, stats


def est_features(cur: CiqEst, p1: CiqEst | None, p3: CiqEst | None, state: pd.DataFrame, month: pd.Timestamp, permnos: pd.Series) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Consensus features for decision `month` for the given permnos. `state` is the CRSP state used for matching. Returns (frame with permno, EST_COLS, same_day), match statistics of `cur`."""
    c, stats = _by_permno(cur, state)
    out = pd.DataFrame({"permno": permnos.drop_duplicates().to_numpy()}).set_index("permno")
    row = c.reindex(out.index)
    fresh = row["mean"].notna() & ((month - row["estdate"]).dt.days <= WINDOW_DAYS) & ((month - row["estdate"]).dt.days >= 0)
    mean, num = row["mean"].where(fresh), row["num"].where(fresh)
    out["numest_fy1"] = num
    out["disp_fy1"] = (row["std"] / row["mean"].abs().where(row["mean"] != 0)).where(fresh)

    def lag(est: CiqEst | None) -> tuple[pd.Series, pd.Series]:
        if est is None:
            return pd.Series(np.nan, index=out.index), pd.Series(np.nan, index=out.index)
        p, _ = _by_permno(est, state)
        q = p.reindex(out.index)
        return q["mean"], q["num"]
    m1, _ = lag(p1)
    m3, n3 = lag(p3)
    out["rev_fy1_1m"] = ((mean - m1) / m1.abs().where(m1 != 0)).where(fresh)
    out["rev_fy1_3m"] = ((mean - m3) / m3.abs().where(m3 != 0)).where(fresh)
    out["numest_chg_3m"] = (num - n3).where(fresh)
    out["same_day"] = (fresh & (row["age"] == 0)).fillna(False).astype(bool)
    stats["fresh_stocks"] = int(fresh.sum())
    return out.reset_index(), stats
