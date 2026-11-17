"""Analyst-estimate, surprise, 13F and ESG features for bridge months, from the engine's own point-in-time tables.

These tables (ibes_cons_pit, ibes_surp_pit, ibes_salsurp_pit, io_pit, esg_pit) are keyed by permno and already hold everything known up to their last knowledge date, which is later than
the last CRSP month for some of them. The engine's as-of rule (panel/features/asof.py) is applied unchanged: for decision month M take the record with the greatest knowledge_date strictly
before M and keep it only if it is at most the family's admissible number of days old. Outside the window the whole family stays empty, which is the honest answer, not a gap to fill.
What is NOT covered: the IBES snapshot of 2026-08-20 is in the raw file but the engine cannot map its CUSIPs to permnos (CRSP ends 2026-06-30 and mapping allows only a short gap), so it is
not in ibes_*_pit; the 2026-07 estimate snapshot is the latest one.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from quant.features.ciqfill.agreement import equal_share

# family -> (pit table, admissible days, value columns); the days match panel/features/asof.py ADMISSIBLE
FAMILIES = {
    "ibes_cons": ("ibes_cons_pit", 45, ["numest_fy1", "disp_fy1", "rev_fy1_1m", "rev_fy1_3m", "updown_fy1", "fy2_growth", "numest_chg_3m", "rev_sal_fy1_1m"]),
    "ibes_surp": ("ibes_surp_pit", 120, ["surprise_sym", "surprise_disp", "pos_surprise_4q", "anndats_act"]),
    "ibes_salsurp": ("ibes_salsurp_pit", 120, ["surprise_sym"]),
    "io": ("io_pit", 120, ["io_frac", "io_n_mgr", "io_hhi", "io_stale_frac", "io_frac_chg", "io_breadth_chg", "io_buy_sell", "io_net_change"]),
    "esg": ("esg_pit", 548, ["esg_rating", "esg_rating_chg"]),
}
RENAME = {"ibes_salsurp": {"surprise_sym": "sales_surprise_sym"}}
SLACK_DAYS = 5


def asof_permno(pit: pd.DataFrame, permnos: pd.Series, month: pd.Timestamp, family: str) -> pd.DataFrame:
    """One row per permno: `kd_<family>` plus the family's panel columns (`days_since_ann` instead of `anndats_act` for ibes_surp). NaN outside the admissible window."""
    _, days, cols = FAMILIES[family]
    p = pit[pit["knowledge_date"] < month].sort_values(["permno", "knowledge_date"]).drop_duplicates("permno", keep="last")
    j = pd.DataFrame({"permno": permnos.drop_duplicates().to_numpy()}).merge(p, on="permno", how="left")
    ok = j["knowledge_date"].notna() & ((month - pd.to_datetime(j["knowledge_date"])).dt.days <= days)
    out = pd.DataFrame({"permno": j["permno"].to_numpy(), f"kd_{family}": pd.to_datetime(j["knowledge_date"]).where(ok).to_numpy()})
    for c in cols:
        if c == "anndats_act":
            out["days_since_ann"] = (month - pd.to_datetime(j[c])).dt.days.where(ok).astype(float).to_numpy()
        else:
            out[RENAME.get(family, {}).get(c, c)] = j[c].where(ok).to_numpy(dtype=float)
    return out


def panel_columns(family: str) -> list[str]:
    """The panel columns of a family: its knowledge date, then its values (renamed as in the panel)."""
    _, _, cols = FAMILIES[family]
    return [f"kd_{family}"] + ["days_since_ann" if c == "anndats_act" else RENAME.get(family, {}).get(c, c) for c in cols]


def replication(rebuilt: pd.DataFrame, engine: pd.DataFrame, family: str) -> dict[str, dict[str, Any]]:
    """Compare the rebuilt value columns with the engine's for the same month and stocks (kd_ columns are compared as dates)."""
    res: dict[str, dict[str, Any]] = {}
    j = engine.merge(rebuilt, on="permno", suffixes=("", "_re"))
    for c in panel_columns(family):
        e, r = j[c], j[c + "_re"]
        both = e.notna() & r.notna()
        res[c] = {"engine_nonnull": int(e.notna().sum()), "rebuilt_nonnull_where_engine_has": int(both.sum()), "equal_share": equal_share(e, r, both, c.startswith("kd_"), atol=1e-9)}
    return res


def replicates(rep: dict[str, dict[str, Any]]) -> bool:
    """The replication rule: every column the engine fills is rebuilt on at least 99% of its rows and agrees on at least 99% of them (an empty engine column passes)."""
    return all(v["engine_nonnull"] == 0 or (v["equal_share"] is not None and v["equal_share"] >= 0.99 and v["rebuilt_nonnull_where_engine_has"] >= 0.99 * v["engine_nonnull"]) for v in rep.values())
