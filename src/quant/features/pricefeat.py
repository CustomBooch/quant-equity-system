"""BRIDGE S4: the price and volume features rebuilt from daily Yahoo data, following panel/features/price.py (BUILD_SPEC section 10). spread_1m has no free source and is not built.

monthly_stats(daily, mkt) -> per (id, calendar month) sums and counts; features(stats, shares, decision_month) -> the 20 features for that decision month (data through the end of the previous month).
`daily` columns: id, date, close (split-adjusted, no dividends), adjclose (total return), volume. `mkt`: Series of daily market total returns indexed by date. `shares`: Series by id (current shares) or a DataFrame (month x id).
ret_36_13 needs 36 months and is taken from the licensed monthly returns by the caller (`ret_36_13_from_monthly`).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.core import kernels as KN

FEATURES = ["ret_1m", "ret_3m", "ret_12_2", "max_ret_1m", "hi52_prox", "vol_1m", "vol_3m", "skew_3m", "beta_12m", "ivol_3m", "amihud_1m", "amihud_3m", "log_dvol_1m", "turnover_1m", "abn_turnover",
            "zero_vol_frac_1m", "log_me", "log_price", "div_yield_12m"]


def monthly_stats(daily: pd.DataFrame, mkt: pd.Series) -> dict[str, pd.DataFrame]:
    """Per (calendar month x id): the sums, counts, extremes and last values the features are built from (`n_r`, `s1`, `lr`, `vol`, `price`, ...)."""
    d = daily.sort_values(["id", "date"]).copy()
    g = d.groupby("id")
    d["ret"] = g["adjclose"].pct_change(fill_method=None)
    d["retx"] = g["close"].pct_change(fill_method=None)
    d["m"] = d["date"].dt.to_period("M")
    d["x"] = d["date"].map(mkt)
    ok = d["ret"].notna() & d["x"].notna()
    d["r_ok"] = d["ret"].where(d["ret"].notna())
    d["xy"] = d["ret"].where(ok)
    d["xx"] = d["x"].where(ok)
    d["lr"] = np.log1p(d["ret"])
    d["lrx"] = np.log1p(d["retx"])
    d["dv"] = d["close"] * d["volume"]
    pos = (d["volume"] > 0) & (d["close"] > 0) & d["ret"].notna()
    d["ami"] = (d["ret"].abs() / d["dv"]).where(pos)
    d["pos"] = pos.astype(float)
    d["zero"] = (d["volume"] == 0).astype(float)
    d["one"] = 1.0
    d["r2"], d["r3"] = d["ret"] ** 2, d["ret"] ** 3
    d["sxy"], d["sxx"], d["syy"] = d["xy"] * d["xx"], d["xx"] ** 2, d["xy"] ** 2
    d["nxy"] = ok.astype(float)
    d["nr"] = d["ret"].notna().astype(float)
    d["nrx"] = d["retx"].notna().astype(float)
    grp = d.groupby(["id", "m"])
    out = {
        "n_r": grp["nr"].sum(), "s1": grp["r_ok"].sum(), "s2": grp["r2"].sum(), "s3": grp["r3"].sum(), "mx": grp["r_ok"].max(),
        "n_xy": grp["nxy"].sum(), "sx": grp["xx"].sum(), "sy": grp["xy"].sum(), "sxx": grp["sxx"].sum(), "sxy": grp["sxy"].sum(), "syy": grp["syy"].sum(),
        "ami_s": grp["ami"].sum(), "ami_n": grp["pos"].sum(), "n_retx": grp["nrx"].sum(),
        "lr": grp["lr"].sum(), "lrx": grp["lrx"].sum(), "n_days": grp["one"].sum(), "zero": grp["zero"].sum(), "dv": grp["dv"].sum(), "vol": grp["volume"].sum(),
        "price": grp["close"].last(), "maxclose": grp["close"].max(),
    }
    return {k: v.unstack("id").sort_index() for k, v in out.items()}


def features(S: dict[str, pd.DataFrame], shares: pd.Series | pd.DataFrame, decision_month: str) -> pd.DataFrame:
    """The FEATURES (without ret_36_13) of every id for `decision_month` ("YYYY-MM"), from monthly_stats through the end of the month before it.
    `shares`: current shares by id, or month x id. NaN where a feature's minimum number of days is not met."""
    M = pd.Period(decision_month, "M")
    m = M - 1
    idx = pd.period_range(S["n_r"].index.min(), m, freq="M")
    S = {k: v.reindex(idx) for k, v in S.items()}
    # Only the row of month m is returned. Rolling windows (and the shifted turnover baseline) need the whole history, so they run on the full frames
    # (quant.core.kernels.rolling: bit-identical to pandas, Rust when built) and are then cut to row m; every other step is element-wise and runs on row m only,
    # which gives the same numbers as computing the full frames first (2026-10-25; the assembled master panel hashes the same).

    def roll(x: pd.DataFrame, w: int) -> pd.Series:
        """x.rolling(w, min_periods=w).sum() at row m."""
        return KN.rolling(x, w, w).loc[m]
    sh = shares.reindex(S["vol"].columns) if isinstance(shares, pd.Series) else shares.reindex(idx)
    turn = S["vol"] / sh if isinstance(sh, pd.DataFrame) else S["vol"].div(sh, axis=1)
    turn = turn.where(S["n_days"] > 0)
    s = {k: v.loc[m] for k, v in S.items()}
    me_price = s["price"]
    f: dict[str, pd.Series] = {}
    f["ret_1m"] = np.expm1(s["lr"])
    f["ret_3m"] = np.expm1(roll(S["lr"], 3))
    f["ret_12_2"] = np.expm1(KN.rolling(S["lr"].shift(1), 11, 11).loc[m])
    f["max_ret_1m"] = s["mx"].where(s["n_r"] >= 15)
    f["vol_1m"] = np.sqrt(((s["s2"] - s["s1"] ** 2 / s["n_r"]) / (s["n_r"] - 1)).clip(lower=0)).where(s["n_r"] >= 15)
    n3, s1, s2, s3 = (roll(S[k], 3) for k in ("n_r", "s1", "s2", "s3"))
    f["vol_3m"] = np.sqrt(((s2 - s1 ** 2 / n3) / (n3 - 1)).clip(lower=0)).where(n3 >= 50)
    var3 = s2 / n3 - (s1 / n3) ** 2
    f["skew_3m"] = ((s3 / n3 - 3 * (s1 / n3) * (s2 / n3) + 2 * (s1 / n3) ** 3) / var3.where(var3 > 0) ** 1.5).where(n3 >= 50)
    nb, sxb, syb, sxxb, sxyb = (roll(S[k], 12) for k in ("n_xy", "sx", "sy", "sxx", "sxy"))
    vx = sxxb - sxb ** 2 / nb
    f["beta_12m"] = ((sxyb - sxb * syb / nb) / vx.where(vx > 0)).where(nb >= 150)
    ni, sxi, syi, sxxi, sxyi, syyi = (roll(S[k], 3) for k in ("n_xy", "sx", "sy", "sxx", "sxy", "syy"))
    vxi = sxxi - sxi ** 2 / ni
    resid = ((syyi - syi ** 2 / ni) - (sxyi - sxi * syi / ni) ** 2 / vxi.where(vxi > 0)) / (ni - 2)
    f["ivol_3m"] = np.sqrt(resid.clip(lower=0)).where(ni >= 50)
    f["amihud_1m"] = (s["ami_s"] / s["ami_n"] * 1e6).where(s["ami_n"] >= 10)
    a3n = roll(S["ami_n"], 3)
    f["amihud_3m"] = (roll(S["ami_s"], 3) / a3n * 1e6).where(a3n >= 30)
    f["log_dvol_1m"] = np.log(s["dv"].where(s["dv"] > 0))
    t = turn.loc[m]
    f["turnover_1m"] = t
    base = KN.rolling(turn.shift(1), 11, 9, "mean").loc[m]
    f["abn_turnover"] = np.log(t / base).where((t > 0) & (base > 0))
    f["zero_vol_frac_1m"] = (s["zero"] / s["n_days"]).where(s["n_days"] > 0)
    f["log_price"] = np.log(me_price.where(me_price > 0))
    shm = sh.loc[m] if isinstance(sh, pd.DataFrame) else pd.Series(sh.to_numpy(float), index=sh.index)
    me = me_price * shm
    f["log_me"] = np.log(me.where(me > 0))
    f["div_yield_12m"] = roll(np.expm1(S["lr"]) - np.expm1(S["lrx"]), 12)
    hi = KN.rolling(S["maxclose"], 12, 12, "max").loc[m]
    f["hi52_prox"] = (me_price / hi).where(roll(S["n_retx"], 12) >= 200)
    return pd.DataFrame(f)[FEATURES].rename_axis("id")


def ret_36_13_from_monthly(ret_total: pd.DataFrame, decision_month: str) -> pd.Series:
    """ret_total: month (Period) x id monthly total returns from the licensed file. Product over months M-36..M-13 with all 24 present."""
    m = pd.Period(decision_month, "M") - 1
    w = ret_total.loc[m - 35: m - 12]
    return np.expm1(np.log1p(w).sum()).where(w.notna().sum() == 24)
