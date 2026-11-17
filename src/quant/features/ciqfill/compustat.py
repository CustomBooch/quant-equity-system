"""The quarterly-history fundamentals for bridge months, from the engine's own Compustat tables (fund_pit), not from Capital IQ.

gp_at_chg, sue_ts, earn_chg_me (and the rest of the engine's `fund` family) need quarterly history: the change against four quarters earlier and the spread of that change over eight
quarters. The build's Compustat quarterlies already run past the last CRSP month (they carry filings up to the data pull), so no Capital IQ history is needed. This applies the engine's
as-of rule (panel/features/asof.py) to those tables: for decision month M take the record with the greatest knowledge_date strictly before M, keep it only if it is at most 200 days old,
and scale earnings change by market equity (`earn_chg_me = earn_chg * 1e6 / ME`, ME = market equity at M-1, which is the bridge row's me_lag).
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from quant.features.ciqfill.agreement import equal_share

ADMISSIBLE_DAYS = 200
FAMILY = ["roa", "roe", "asset_growth", "sales_growth", "accruals", "cash_at", "rd_intensity", "sga_intensity", "capex_at", "gp_at_chg", "roa_chg", "sue_ts"]
PIT_COLS = ["gvkey", "knowledge_date", "datadate", "rdq", "earn_chg"] + FAMILY
CHECKED = ["gp_at_chg", "sue_ts", "earn_chg_me"]


def asof_family(pit: pd.DataFrame, link: pd.DataFrame, month: pd.Timestamp, me: pd.Series) -> pd.DataFrame:
    """`pit`: fund_pit rows (PIT_COLS). `link`: permno, gvkey. `me`: market equity in dollars, indexed by permno. One row per permno in `link`."""
    p = pit[pit["knowledge_date"] < month].sort_values(["gvkey", "knowledge_date"]).drop_duplicates("gvkey", keep="last")
    p = p[(month - pd.to_datetime(p["knowledge_date"])).dt.days <= ADMISSIBLE_DAYS]
    j = link.drop_duplicates("permno").merge(p, on="gvkey", how="left")
    ok = j["knowledge_date"].notna()
    out = pd.DataFrame({"permno": j["permno"].to_numpy()})
    for c in FAMILY:
        out[c] = j[c].to_numpy(dtype=float)
    m = out["permno"].map(me).to_numpy(dtype=float)
    out["earn_chg_me"] = np.where(m > 0, j["earn_chg"].to_numpy(dtype=float) * 1e6 / np.where(m > 0, m, 1.0), np.nan)
    out["kd_fund"] = pd.to_datetime(j["knowledge_date"]).where(ok)
    out["days_since_rdq"] = (month - pd.to_datetime(j["rdq"])).dt.days.where(ok).astype(float)
    return out


def replication(rebuilt: pd.DataFrame, engine: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """Compare the rebuilt checked features with the engine's for the same month and stocks. `engine` has permno plus CHECKED columns."""
    j = engine.merge(rebuilt[["permno"] + CHECKED], on="permno", suffixes=("", "_re"))
    res = {}
    for c in CHECKED:
        e, r = j[c], j[c + "_re"]
        both = e.notna() & r.notna()
        res[c] = {"engine_nonnull": int(e.notna().sum()), "rebuilt_nonnull_where_engine_has": int(both.sum()), "equal_share": equal_share(e, r, both, dates=False, atol=1e-9)}
    return res
