"""Use Capital IQ fundamentals where the engine has none.

engine_flags  the engine's universe rule (panel/universe/flags.py) applied to a security_state month, so a bridge month gets the same in_research / in_investable / rank flags
fill_month    a decision month the engine DID build: fill a cell only where the engine value is missing; never overwrite; flag every filled cell (`<feature>_ciq`)
bridge_rows   decision months the engine cannot build (it needs CRSP trading in the month): one row per research-universe stock, with market data, the six Capital IQ
              fundamentals where the company matches, every other feature left empty, `bridge` = True. These rows are for scoring forward, never for training.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.features.ciqfill.features import FEATURES

MIN_PRICE = 5.0
NYSE_PCTL = 0.20
MIN_VOLUME_DAYS = 10


def _ranks(me: pd.Series, mask: pd.Series, permno: pd.Series) -> pd.Series:
    """row_number() over (partition by mask order by me desc, permno) for rows where mask is true; NaN elsewhere (the engine's me_rank)."""
    order = pd.DataFrame({"me": me, "permno": permno})[mask].sort_values(["me", "permno"], ascending=[False, True])
    r = pd.Series(np.nan, index=me.index)
    r.loc[order.index] = np.arange(1, len(order) + 1)
    return r


def engine_flags(state: pd.DataFrame) -> pd.DataFrame:
    """`state`: security_state rows for one month (permno, price, me, exch, sectype, shrtype, pos_vol_days). Adds in_research, in_investable, me_rank, in_top500, in_top1000."""
    s = state.reset_index(drop=True).copy()
    s["in_research"] = ((s["sectype"] == "EQTY") & (s["shrtype"] == "NS") & s["exch"].isin(["N", "A", "Q"]) & s["price"].notna() & (s["me"] > 0)).astype(bool)
    priced = s["in_research"] & (s["price"] >= MIN_PRICE)
    nyse = s.loc[s["in_research"] & (s["exch"] == "N"), "me"]
    p20 = nyse.quantile(NYSE_PCTL) if len(nyse) else np.nan
    s["in_investable"] = (priced & (s["me"] >= p20) & (s["pos_vol_days"] >= MIN_VOLUME_DAYS)).astype(bool)
    s["me_rank"] = _ranks(s["me"], priced, s["permno"])
    s["in_top500"] = (s["me_rank"] <= 500).fillna(False).astype(bool)
    s["in_top1000"] = (s["me_rank"] <= 1000).fillna(False).astype(bool)
    return s


def fill_month(panel_m: pd.DataFrame, feats: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, dict[str, int]]]:
    """`panel_m`: one month of the engine panel. `feats`: permno plus the six Capital IQ features, one row per permno. Returns the filled month and per-feature counts."""
    out = panel_m.copy()
    f = feats.drop_duplicates("permno").set_index("permno")
    stats: dict[str, dict[str, int]] = {}
    for c in FEATURES:
        was_missing = out[c].isna()
        civ = out["permno"].map(f[c])
        gap = was_missing & civ.notna()
        out.loc[gap, c] = civ[gap]
        out[c + "_ciq"] = gap.to_numpy()
        stats[c] = {"engine_missing": int(was_missing.sum()), "filled": int(gap.sum()), "still_missing": int((was_missing & ~gap).sum())}
    out["bridge"] = False
    return out, stats


def bridge_rows(t: pd.DataFrame, feats: pd.DataFrame, flags: pd.DataFrame, month: pd.Timestamp, exact_state: bool) -> pd.DataFrame:
    """`t`: matched CiqMonth table (permno, mcap, close, shares, ...). `feats`: its six features (same index). `flags`: engine_flags() of the latest security_state month.
    `exact_state`: the state month-end IS the Capital IQ pricing date, so market data and flags come straight from CRSP state (the engine's own values); otherwise market data
    come from Capital IQ and the flags are recomputed with the engine rule on those values. Rows: research-universe stocks that are not delisted (and, when not exact, listed in Capital IQ)."""
    base = flags[flags["in_research"] & ~flags["delisted"].fillna(False).astype(bool)].copy() if "delisted" in flags else flags[flags["in_research"]].copy()
    c = pd.concat([t, feats[FEATURES]], axis=1)
    c = c[c["permno"].notna() & (c["mcap"] > 0) & (c["close"] > 0)].copy()
    c["permno"] = c["permno"].astype("int64")
    c = c.sort_values("mcap", ascending=False).drop_duplicates("permno").set_index("permno")
    base = base.set_index("permno", drop=False)
    if exact_state:
        d = base
        me, price, shares = d["me"], d["price"], d["shares"]
        flag_src = d
    else:
        d = base[base.index.isin(c.index)].copy()
        d["me"], d["price"], d["shares"] = c["mcap"].reindex(d.index), c["close"].reindex(d.index), c["shares"].reindex(d.index)
        flag_src = engine_flags(d.reset_index(drop=True).assign(sectype=d["sectype"].to_numpy(), shrtype=d["shrtype"].to_numpy())).set_index("permno", drop=False)
        me, price, shares = flag_src["me"], flag_src["price"], flag_src["shares"]
    out = pd.DataFrame({"permno": flag_src.index.astype("int64"), "month": month, "is_holdout": True, "in_full_panel": True, "in_research": True,
                        "in_investable": flag_src["in_investable"].to_numpy(), "me_lag": me.to_numpy(), "price_lag": price.to_numpy(), "shares_lag": shares.to_numpy(),
                        "log_me": np.log(me.to_numpy()), "log_price": np.log(price.to_numpy()), "me_rank": flag_src["me_rank"].to_numpy(),
                        "in_top500": flag_src["in_top500"].to_numpy(), "in_top1000": flag_src["in_top1000"].to_numpy()})
    if "siccd" in d:
        out["sic2"] = (d["siccd"] // 100).to_numpy()
    idx = out["permno"].to_numpy()
    for f in FEATURES:
        out[f] = c[f].reindex(idx).to_numpy()
        out[f + "_ciq"] = ~np.isnan(out[f].to_numpy(dtype=float))
    for h in ("1m", "3m", "6m", "12m"):
        out[f"fwd_incomplete_{h}"] = True
    out["bridge"] = True
    out["bridge_market_source"] = "crsp_state" if exact_state else "capital_iq"
    out["ciq_matched"] = pd.Series(idx).isin(c.index).to_numpy()
    out["ciq_late_filing"] = c["late_filing"].reindex(idx).fillna(False).to_numpy(dtype=bool)
    return out.reset_index(drop=True)
