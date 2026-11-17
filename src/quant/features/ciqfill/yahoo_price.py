"""Price and volume features for bridge months from the free Yahoo daily bars, using the BRIDGE workstream's validated builder (quant.features.pricefeat, unchanged).

    load_yahoo          one .json.gz chart file per permno (data/external/free_2026/yahoo): daily close, split-adjusted close with dividends, volume
    last_close          the latest non-missing close of one stock and its date (the target book's share prices)
    market_proxy        daily market return built from the same stocks, market-equity weighted; CRSP's market return ends 2026-06-30 and a network download is not needed
    price_features      the engine's price features for a decision month (data through the end of the previous month)
    ret_36_13           product of monthly returns 36 to 13 months back, from the panel's own monthly returns (months up to 2026-06 are all that this needs)

Parity is checked against the engine's CRSP-built values for the last engine months with the BRIDGE rule registered in its S2 specification: coverage of at least 70% of the engine's rows and a median
within-month rank correlation of at least 0.90; a feature is filled only if its own check passed. Nothing is overwritten. spread_1m has no free source and is not built.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from quant.features import pricefeat as PF
from quant.data.free.archive import MIN_COVERAGE as MIN_COVERAGE, MIN_RANK_CORR as MIN_RANK_CORR      # the BRIDGE S2 acceptance rule, owned by the BRIDGE package (re-exported for assemble)

MIN_PROXY_CORR = 0.98
FEATURES = [f for f in PF.FEATURES] + ["ret_36_13"]
FILL_SKIP = {"log_me", "log_price"}                       # already present in every bridge row (from the CRSP state or Capital IQ), never overwritten
CLIP = 0.5                                                # a daily return beyond +-50% in the market proxy is treated as a data error


def load_yahoo(permnos: Iterable[int], root: Path) -> pd.DataFrame:
    """Daily bars (date, close, volume, adjclose, id) of every permno with a readable chart file in `root`; a missing or malformed file is skipped."""
    frames = []
    for p in permnos:
        f = Path(root) / f"{int(p)}.json.gz"
        if not f.exists():
            continue
        try:
            with gzip.open(f) as g:
                r = json.load(g)["chart"]["result"][0]
            q = r["indicators"]["quote"][0]
            df = pd.DataFrame({"date": pd.to_datetime(r["timestamp"], unit="s").normalize(), "close": q["close"], "volume": q["volume"], "adjclose": r["indicators"]["adjclose"][0]["adjclose"]})
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        df = df.dropna(subset=["close", "adjclose"]).drop_duplicates("date", keep="last")
        df["id"] = int(p)
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", "close", "volume", "adjclose", "id"])


def last_close(permno: int, root: Path) -> tuple[float, dt.date | None]:
    """(close, date) of the newest bar with a close in <root>/<permno>.json.gz; (nan, None) if there is no file or no close."""
    f = Path(root) / f"{int(permno)}.json.gz"
    if not f.exists():
        return np.nan, None
    with gzip.open(f) as g:
        r = json.load(g)["chart"]["result"][0]
    for c, t in zip(reversed(r["indicators"]["quote"][0]["close"]), reversed(r["timestamp"]), strict=True):     # unequal lengths would pair a close with the wrong day
        if c is not None:
            return float(c), pd.to_datetime(t, unit="s").date()
    return np.nan, None


def market_proxy(daily: pd.DataFrame, me: dict[pd.Period, pd.Series]) -> pd.Series:
    """Daily market-equity-weighted mean of the stocks' total returns. A day in month p is weighted by market equity at the end of month p-1 (the latest state month if p-1 is later than the last one)."""
    d = daily.sort_values(["id", "date"]).copy()
    d["ret"] = d.groupby("id")["adjclose"].pct_change(fill_method=None)            # type: ignore[call-arg]  # pandas 2 keyword; pandas-stubs follow pandas 3
    d.loc[d["ret"].abs() > CLIP, "ret"] = np.nan
    r = d.pivot(index="date", columns="id", values="ret")
    last = max(me)
    per = cast(pd.DatetimeIndex, r.index).to_period("M")
    w = pd.DataFrame(index=r.index, columns=r.columns, dtype=float)
    for p in per.unique():
        src = me.get(p - 1, me[last] if p - 1 > last else None)
        if src is None:
            continue
        w.loc[per == p] = np.tile(src.reindex(r.columns).to_numpy(float), (int((per == p).sum()), 1))
    ok = r.notna() & w.notna() & (w > 0)
    return (r.where(ok) * w.where(ok)).sum(axis=1) / w.where(ok).sum(axis=1).replace(0, np.nan)


def price_features(daily: pd.DataFrame, mkt: pd.Series, shares: pd.DataFrame, decision_months: list[str], ret_total: pd.DataFrame | None) -> dict[str, pd.DataFrame]:
    """Features per decision month (permno index, columns FEATURES). `shares`: month (Period) x permno shares outstanding. `ret_total`: month (Period) x permno monthly returns for ret_36_13."""
    S = PF.monthly_stats(daily, mkt)
    out = {}
    for m in decision_months:
        f = PF.features(S, shares, m)
        f["ret_36_13"] = PF.ret_36_13_from_monthly(ret_total, m).reindex(f.index) if ret_total is not None else np.nan
        out[m] = f[FEATURES]
    return out


def parity(built: dict[str, pd.DataFrame], engine: dict[str, pd.DataFrame], features: list[str] | None = None) -> dict[str, dict[str, Any]]:
    """Per feature: mean coverage of the engine's non-empty rows and the median within-month Spearman correlation, over the decision months present in both;
    `pass` when both meet the BRIDGE S2 rule (MIN_COVERAGE, MIN_RANK_CORR)."""
    from scipy.stats import spearmanr
    res: dict[str, dict[str, Any]] = {}
    for c in (features or FEATURES):
        cov: list[float] = []
        rho: list[float] = []
        for m, e in engine.items():
            if m not in built or c not in e:
                continue
            e_c = e[c].dropna()
            b = built[m][c].reindex(e_c.index)
            both = b.notna()
            cov.append(float(both.mean()) if len(e_c) else np.nan)
            if both.sum() >= 30:
                if (e_c[both] != e_c[both].mode().iloc[0]).sum() < 30:          # a near-constant feature (e.g. zero-volume days among investable stocks): rank correlation is meaningless,
                    rho.append(1.0 if np.isclose(e_c[both], b[both], atol=1e-9).mean() >= 0.99 else 0.0)        # so the test is "at least 99% exactly equal"
                else:
                    rho.append(float(spearmanr(e_c[both], b[both])[0]))
        cover = round(float(np.nanmean(cov)), 4) if cov else None
        median = round(float(np.median(rho)), 4) if rho else None
        res[c] = {"coverage_of_engine_rows": cover, "median_rank_corr": median, "months": len(rho),
                  "pass": bool(cover is not None and median is not None and cover >= MIN_COVERAGE and median >= MIN_RANK_CORR)}
    return res
