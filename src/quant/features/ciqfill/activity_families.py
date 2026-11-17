"""Insider (Form 4) and fails-to-deliver features for bridge months, with the engine's definitions (panel/features/activity.py).

insider: events with knowledge_date in [M - w, M) (the engine's age = M - knowledge_date, at most w days). Net value 6m = (open-market purchases - sales) / ME over 182 days; distinct buyers 6m;
         any purchase 3m (91 days); net transaction-count ratio 12m (365 days). A count is 0 only where the source covers the security and the whole window: the company has a CIK link and
         M - (w + 1) >= filing_start and M - 2 <= filing_end; otherwise the feature is empty.
ftd:     the latest fails-to-deliver month known before M (knowledge = month end + 30 days, at most 62 days old), mean daily fails / shares outstanding at that month's CRSP state, and its
         change from the month before. Months whose CRSP state does not exist (after 2026-06) cannot be computed, so those rows stay empty rather than use another share count.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from quant.features.ciqfill.agreement import equal_share

INSIDER_COLS = ["ins_net_value_6m", "ins_n_buyers_6m", "ins_buy_3m", "ins_net_ratio_12m"]
FTD_COLS = ["kd_ftd", "ftd_frac", "ftd_frac_chg"]
EDGAR_COLS = ["n_8k_90d", "abn_8k", "n_8k_502_365d", "n_nt_365d", "n_sc13d_365d"]


def insider_features(tx: pd.DataFrame, buyers: pd.DataFrame, cov: tuple[pd.Timestamp, pd.Timestamp], has_cik: pd.Series, me: pd.Series, month: pd.Timestamp) -> pd.DataFrame:
    """`tx`: permno, knowledge_date, code ('P'/'S'), value (events before `month`). `buyers`: permno, knowledge_date, owner_cik. `has_cik`: bool by permno (also defines the rows).
    `me`: market equity in dollars by permno. One row per permno in `has_cik`."""
    start, end = cov
    t = tx[(tx["knowledge_date"] < month)].copy()
    t["age"] = (month - pd.to_datetime(t["knowledge_date"])).dt.days
    t = t[t["age"] <= 365]
    b = buyers[(buyers["knowledge_date"] < month)].copy()
    b["age"] = (month - pd.to_datetime(b["knowledge_date"])).dt.days
    b = b[b["age"] <= 182]
    p6, s6 = t[(t["code"] == "P") & (t["age"] <= 182)], t[(t["code"] == "S") & (t["age"] <= 182)]
    out = pd.DataFrame({"permno": has_cik.index.to_numpy()}).set_index("permno")
    out["buy6"] = p6.groupby("permno")["value"].sum(min_count=1)
    out["sell6"] = s6.groupby("permno")["value"].sum(min_count=1)
    out["nb3"] = t[(t["code"] == "P") & (t["age"] <= 91)].groupby("permno").size()
    out["nb12"] = t[t["code"] == "P"].groupby("permno").size()
    out["ns12"] = t[t["code"] == "S"].groupby("permno").size()
    out["buyers6"] = b.groupby("permno")["owner_cik"].nunique()
    out = out.fillna({"nb3": 0, "nb12": 0, "ns12": 0, "buyers6": 0, "buy6": 0.0, "sell6": 0.0})
    ck = has_cik.reindex(out.index).fillna(False).to_numpy(dtype=bool)

    def ok(window_days: int) -> np.ndarray:
        """Stocks with a CIK link, and only if the source covers the whole window: M - (w + 1) >= filing_start and M - 2 <= filing_end."""
        return ck & (month - pd.Timedelta(days=window_days + 1) >= start) & (month - pd.Timedelta(days=2) <= end)
    m = me.reindex(out.index).to_numpy(dtype=float)
    r = pd.DataFrame(index=out.index)
    r["ins_net_value_6m"] = np.where(ok(182) & (m > 0), (out["buy6"] - out["sell6"]) / np.where(m > 0, m, 1.0), np.nan)
    r["ins_n_buyers_6m"] = np.where(ok(182), out["buyers6"], np.nan)
    r["ins_buy_3m"] = np.where(ok(91), (out["nb3"] > 0).astype(float), np.nan)
    tot = out["nb12"] + out["ns12"]
    r["ins_net_ratio_12m"] = np.where(ok(365) & (tot > 0), (out["nb12"] - out["ns12"]) / np.where(tot > 0, tot, 1.0), np.nan)
    return r.reset_index()


def ftd_features(ftd: pd.DataFrame, shares: dict[pd.Timestamp, pd.Series], cov: tuple[pd.Timestamp, pd.Timestamp], permnos: pd.Series, month: pd.Timestamp) -> pd.DataFrame:
    """`ftd`: permno, fmonth, fails_mean. `shares`: shares outstanding by permno for each available CRSP state month (key = month start). `cov`: first and last FTD month."""
    first, last = cov
    out = pd.DataFrame({"permno": permnos.drop_duplicates().to_numpy()})
    for c in FTD_COLS:
        out[c] = np.nan
    cand = [d for d in pd.date_range(first, last, freq="MS") if (d + pd.offsets.MonthEnd(0) + pd.Timedelta(days=30)) < month]
    if not cand:
        return out
    fm = cand[-1]
    kd = fm + pd.offsets.MonthEnd(0) + pd.Timedelta(days=30)
    if (month - kd).days > 62 or fm not in shares:
        return out

    def frac(m: pd.Timestamp) -> pd.Series:
        sh = shares.get(m)
        if sh is None or m < first:
            return pd.Series(dtype=float)
        f = ftd[ftd["fmonth"] == m].drop_duplicates("permno").set_index("permno")["fails_mean"]
        v = f.reindex(sh.index).fillna(0.0) / sh.where(sh > 0)
        return v
    cur, prev = frac(fm), frac(fm - pd.DateOffset(months=1))
    out["ftd_frac"] = out["permno"].map(cur)
    out["ftd_frac_chg"] = out["ftd_frac"] - out["permno"].map(prev)
    out["kd_ftd"] = pd.Series(kd, index=out.index).where(out["ftd_frac"].notna())
    out["kd_ftd"] = pd.to_datetime(out["kd_ftd"])
    return out


def edgar_features(ev: pd.DataFrame, covered: pd.Series, item_502_first: pd.Timestamp, last_event: pd.Timestamp, month: pd.Timestamp) -> pd.DataFrame:
    """SEC filing counts as in panel/features/activity.py edgar(): 8-Ks in the last 90 days, abnormal 8-Ks (90-day count minus a quarter of the count 91 to 450 days back), Item 5.02 8-Ks, late-filing
    notices (NT) and Schedule 13D filings in the last 365 days. `ev`: permno, knowledge_date, kind ('8k'/'nt'/'sc13d'), item_502. `covered`: bool by permno (the company has a CIK in both the link and the
    EDGAR list; also defines the rows). Empty for uncovered companies and, as the engine does for insider, where the window is not fully covered: `last_event` (the newest event in the table)
    must be at most two days before `month`. The 5.02 count also needs the item to exist 366 days back."""
    e = ev[ev["knowledge_date"] < month].copy()
    e["age"] = (month - pd.to_datetime(e["knowledge_date"])).dt.days
    e = e[e["age"] <= 450]
    k8 = e[e["kind"] == "8k"]
    out = pd.DataFrame(index=covered.index.rename("permno"))
    n90 = k8[k8["age"] <= 90].groupby("permno").size().reindex(out.index).fillna(0)
    base = k8[k8["age"] > 90].groupby("permno").size().reindex(out.index).fillna(0)
    n502 = k8[(k8["age"] <= 365) & k8["item_502"].fillna(False).astype(bool)].groupby("permno").size().reindex(out.index).fillna(0)
    nnt = e[(e["kind"] == "nt") & (e["age"] <= 365)].groupby("permno").size().reindex(out.index).fillna(0)
    n13 = e[(e["kind"] == "sc13d") & (e["age"] <= 365)].groupby("permno").size().reindex(out.index).fillna(0)
    ok = covered.to_numpy(dtype=bool) & bool(month - pd.Timedelta(days=2) <= last_event)
    out["n_8k_90d"] = np.where(ok, n90, np.nan)
    out["abn_8k"] = np.where(ok, n90 - base / 4.0, np.nan)
    out["n_8k_502_365d"] = np.where(ok & bool(month - pd.Timedelta(days=366) >= item_502_first), n502, np.nan)
    out["n_nt_365d"] = np.where(ok, nnt, np.nan)
    out["n_sc13d_365d"] = np.where(ok, n13, np.nan)
    return out.reset_index()


def replication(rebuilt: pd.DataFrame, engine: pd.DataFrame, cols: list[str]) -> dict[str, dict[str, Any]]:
    """Compare the rebuilt columns with the engine's for the same month and stocks (kd_ columns as dates; counts are tighter than ratios, atol 1e-12),
    and count every rebuilt value, so that coverage wider than the engine's shows."""
    j = engine.merge(rebuilt, on="permno", suffixes=("", "_re"))
    res = {}
    for c in cols:
        e, r = j[c], j[c + "_re"]
        both = e.notna() & r.notna()
        res[c] = {"engine_nonnull": int(e.notna().sum()), "rebuilt_nonnull_where_engine_has": int(both.sum()), "rebuilt_nonnull_total": int(r.notna().sum()),
                  "equal_share": equal_share(e, r, both, c.startswith("kd_"), atol=1e-12)}
    return res
