"""PAPER target book from the scores (dry run, no account or broker access). python -m quant.portfolio.targets [--score EXAMPLE] [--aum-usd 100000]
The model's long book (quant.portfolio.model) from the newest scores, mapped to Alpaca symbols, shares at Yahoo's last price for the account value in USD, plus the
short leg (amendment A11) and both legs levered (A12). Writes lab/paper/results/target_book_<score>_<policy>_master_<month>_alpaca.csv and .json; the overlay state is reported.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from quant.data.free import archive as AR
from quant.features.ciqfill.yahoo_price import last_close
from quant.core.weights import capped_weights
from quant.portfolio import schedule as SCH
from quant.portfolio.plan import MIN_ORDER_USD, QTY_DECIMALS          # the order rules of SPEC section 4
from quant.core.paths import REPO, YAHOO_DAILY
from quant.portfolio.broker import BROKER, SHORTS, line_column
from quant.portfolio.model import leverage, long_policy
from quant.data.instruments.crsp import crsp_ids

RES = REPO / "lab/paper/results"
POLICIES = {"vw90": {"entry": 0.90, "exit": 0.70, "cap": 0.10, "sizing": "vw"}, "example": long_policy()}          # example = the model's long book (quant.portfolio.model; SPEC A2/A3); vw90 is the v1 book, kept for the record
PRIMARY = "example"


def trend(px: pd.Series) -> dict[str, Any]:
    """Overlay state from daily adjusted closes: the month of the newest bar counts as incomplete, so the last complete month is the one before it;
    its close against the mean of the last ten month-end closes gives the exposure (quant.portfolio.schedule.overlay_exposure)."""
    me = px.groupby(cast(pd.DatetimeIndex, px.index).to_period("M")).last().iloc[:-1]
    close, mean10 = float(me.iloc[-1]), float(me.rolling(10, min_periods=10).mean().iloc[-1])
    return {"last_complete_month": str(me.index[-1]), "close": close, "mean_10m": mean10, "below_trend": close < mean10, "exposure_now": SCH.overlay_exposure(close, mean10),
            "shadow_exposure_75": SCH.overlay_exposure(close, mean10, 0.75), "shadow_exposure_25": SCH.overlay_exposure(close, mean10, 0.25)}


def trend_state() -> dict[str, Any]:
    import yfinance as yf

    px = yf.download("VTI", period="2y", auto_adjust=False, progress=False)["Adj Close"].squeeze()
    px.index = pd.to_datetime(px.index).tz_localize(None)
    return trend(px)


def scores_live(score: str) -> tuple[pd.DataFrame, str, str]:
    """The BRIDGE live table (the source before amendment A4): score, market cap and last price from the newest archive."""
    day = AR.latest_day("live_scores.parquet")
    sc = pd.read_parquet(day / "live_scores.parquet").set_index("permno")[score]
    an = pd.read_parquet(day / "analyst.parquet").drop_duplicates("permno").set_index("permno")
    d = pd.DataFrame({"score": sc, "mcap": an["market_cap"].reindex(sc.index), "px": an["last_price"].reindex(sc.index)}).dropna(subset=["score"])
    return d, f"live table {day.name}", "2026-10"


def scores_master() -> tuple[pd.DataFrame, str, str]:
    """Amendment A4: the newest quant.models.scores file (EXAMPLE on the current master); market cap at the end of the previous month, the latest local Yahoo close as price."""
    from quant.models import scores as SC

    found = SC.latest()
    if found is None:
        raise SystemExit(f"refused: no score file in {SC.OUT}; run tools/py -m quant.models.scores first")
    month, f = found
    s = pd.read_parquet(f).set_index("permno")
    px = {int(p): last_close(int(p), YAHOO_DAILY)[0] for p in s.index[s["q"] > min(v["entry"] for v in POLICIES.values())]}
    d = pd.DataFrame({"score": s["score"], "mcap": s["me_lag"], "px": pd.Series(px).reindex(s.index).fillna(s["price_lag"].abs())})
    return d, f"scores {f.name}", month[:7]


def short_leg(d: pd.DataFrame, month: str, aum_usd: float, held: set[int] | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Amendment A11: the short basket of `month` (quant.portfolio.shorts) mapped to shortable Alpaca symbols, sized at whole shares toward zero.
    d: the month's scores indexed by permno with score, mcap, px. Returns (rows with line, ticker, w (positive, sum 1), px, permno, info)."""
    from quant.data.instruments import alpaca as AM
    from quant.portfolio import shorts as S  # type: ignore[attr-defined]  # not in the public edition: the example book is long only

    sc = d.reset_index().rename(columns={"index": "permno", "mcap": "me_lag"})[["permno", "me_lag", "score"]]
    sic2, ann = S.load_inputs(pd.Timestamp(f"{month}-01"), sc["permno"])
    b = S.basket(sc, sic2, ann, held)
    n0 = len(b)
    b = b.set_index("permno").join(AM.map_permnos(b["permno"])[["ticker", "line"]])
    unmapped = float(b.loc[b["line"].isna(), "w"].sum())
    b = b[b["line"].notna()].reset_index()
    b, unshortable = S.keep_shortable(b, AM.latest_assets())
    b["px"] = b["permno"].map(d["px"])
    s, rule = S.gradual_short(None)                                                 # no live 6-month basket history yet (A11)
    info = {"short_fraction": s, "short_rule": rule, "short_candidates": n0, "short_names": int(len(b)), "short_weight_unmapped": unmapped,
            "short_weight_unshortable": unshortable, "expected_announcers_excluded": len(ann)}
    return b, info


def select(d: pd.DataFrame, pol: dict[str, Any], held: set[int] | None = None) -> pd.DataFrame:
    """Rows (indexed by permno) with positive market cap whose score percentile `q` is above the policy's entry level, or above its exit level for a
    name in `held` (last month's book): the holding rule of SPEC A3 and the backtest's policy. With nothing held this is the initial book."""
    keep = d.index.isin(list(held or set())) & (d["q"] > pol["exit"])
    out: pd.DataFrame = d[((d["q"] > pol["entry"]) | keep) & (d["mcap"] > 0)].copy()
    return out


def previous_book(stem_prefix: str, month: str, res: Path | None = None, ticker_to_permno: dict[str, int] | None = None) -> tuple[set[int], set[int], str]:
    """(long permnos, short permnos, source) of the newest stored target book for an earlier decision month (files `<stem_prefix><YYYY-MM>_alpaca.csv`).
    A book without a permno column (made before 2026-11-12) is matched through its CRSP tickers when `ticker_to_permno` is given; otherwise nothing is
    held, and the source says so."""
    import glob

    res = RES if res is None else res
    books = sorted(f for f in glob.glob(str(res / f"{stem_prefix}*_alpaca.csv")) if Path(f).stem.removeprefix(stem_prefix)[:7] < month)
    if not books:
        return set(), set(), "no earlier book: initial book (entry rule only)"
    b = pd.read_csv(books[-1])
    if "permno" not in b.columns:
        if not ticker_to_permno:
            return set(), set(), f"{Path(books[-1]).name} has no permno column: holding rule not applied this month"
        b["permno"] = b["ticker"].astype(str).map(ticker_to_permno)
        lost = int(b["permno"].isna().sum())
        b = b.dropna(subset=["permno"])
        note = f"{Path(books[-1]).name} (matched by CRSP ticker; {lost} unmatched)"
    else:
        note = Path(books[-1]).name
    side = b["side"] if "side" in b.columns else pd.Series("long", index=b.index)
    return set(b.loc[side == "long", "permno"].astype(int)), set(b.loc[side == "short", "permno"].astype(int)), note


def raw_weight(mcap: pd.Series, pol: dict[str, Any]) -> pd.Series:
    """Uncapped weight of each name before normalisation: its market cap (vw) or the square root of it (sqrt)."""
    mcap = mcap.astype(float)
    return mcap if pol["sizing"] == "vw" else cast(pd.Series, np.sqrt(mcap))


def size(ok: pd.DataFrame, pol: dict[str, Any], aum_usd: float) -> pd.DataFrame:
    """Weights (raw_weight capped at the policy cap, or at 1/n when fewer names than 1/cap), USD targets, shares to the engine's 4 decimals and order flags,
    for the mapped rows `ok` (columns mcap, px). Sorted by weight, largest first."""
    ok = ok.assign(w=capped_weights(raw_weight(ok["mcap"], pol).to_numpy(), max(pol["cap"], 1.0 / len(ok))))
    ok["usd_target"] = ok["w"] * aum_usd
    ok["shares"] = np.round(ok["usd_target"] / ok["px"], QTY_DECIMALS)
    ok["flags"] = np.where(ok["usd_target"] < MIN_ORDER_USD, "below_min_order", "")
    return ok.sort_values("w", ascending=False)


def map_alpaca(sel: pd.DataFrame) -> pd.DataFrame:
    """Adds cusip, ticker and the Alpaca symbol (`line`, None if there is none) to rows indexed by permno (amendment A10)."""
    from quant.data.instruments import alpaca as AM

    return sel.join(AM.map_permnos(sel.index)[["cusip", "ticker", "line"]])


def map_lines(sel: pd.DataFrame, broker: str = BROKER) -> pd.DataFrame:
    """Rows mapped to the broker's lines (`line_column(broker)`); only Alpaca exists (amendment A14)."""
    line_column(broker)
    return map_alpaca(sel)


def unmapped_share(sel: pd.DataFrame, pol: dict[str, Any], col: str = "line") -> float:
    """Share of the selected names' raw weight that has no broker line (`col` missing): the weight lost before renormalisation (preflight's 5% rail)."""
    w = raw_weight(sel["mcap"], pol)
    return float(w[sel[col].isna()].sum() / w.sum())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", default="EXAMPLE")
    ap.add_argument("--aum-usd", type=float, default=100000.0, help="account value in USD (amendment A10: no FX)")
    ap.add_argument("--long-only", action="store_true", help="Alpaca book without the A11 short leg")
    ap.add_argument("--policy", default=PRIMARY, choices=list(POLICIES))
    ap.add_argument("--source", default="master", choices=["master", "live"], help="master: quant.models.scores (amendment A4); live: the BRIDGE live table")
    a = ap.parse_args(argv)
    d, src, month = scores_master() if a.source == "master" else scores_live(a.score)
    d["q"] = d["score"].rank(pct=True)
    pol = POLICIES[a.policy]
    col = line_column(BROKER)
    stem = f"target_book_{a.score}_{a.policy}" + (f"_master_{month}" if a.source == "master" else "") + "_alpaca"
    if (RES / f"{stem}.csv").exists():
        raise SystemExit(f"refused: {RES / stem}.csv exists; a target book is the record its plans were made from: move it aside to rebuild it")
    if a.source == "master":
        ids = crsp_ids(d.index)
        t2p = {str(t): int(cast(int, p)) for p, t in ids["ticker"].dropna().items()}
        held_long, held_short, held_source = previous_book(f"target_book_{a.score}_{a.policy}_master_", month, ticker_to_permno=t2p)
    else:
        held_long, held_short, held_source = set(), set(), "not applied"
    sel = map_lines(select(d, pol, held_long))
    n_sel = len(sel)
    aum_usd = a.aum_usd
    if not sel[col].notna().any():
        raise SystemExit(f"refused: none of the {n_sel} selected names maps to an Alpaca symbol (is the asset list missing or stale?)")
    ok = size(sel[sel[col].notna()], pol, aum_usd)
    out = ok.assign(permno=ok.index.astype(int))[[col, "permno", "ticker", "score", "q", "mcap", "px", "w", "usd_target", "shares", "flags"]]
    short_info: dict[str, Any] = {}
    if SHORTS and not a.long_only:
        sb, short_info = short_leg(d, month, aum_usd, held_short)
        if overlap := sorted(set(sb["line"]) & set(out[col])):
            raise SystemExit(f"refused: names on both legs: {overlap}")
        s = short_info["short_fraction"]
        k = leverage(s)                                                              # A12: both legs scaled so long + short = the gross cap
        short_info.update({"leverage": k, "long_weight": k * (1 + s), "short_weight": k * s, "gross": k * (1 + 2 * s)})
        out = out.assign(side="long", w=out["w"] * k * (1 + s))
        out["usd_target"] = out["w"] * aum_usd
        out["shares"] = np.round(out["usd_target"] / out["px"], QTY_DECIMALS)
        sh = pd.DataFrame({col: sb["line"], "permno": sb["permno"].astype(int), "ticker": sb["ticker"], "score": sb["score"], "q": np.nan, "mcap": sb["me_lag"], "px": sb["px"], "w": -sb["w"] * k * s, "side": "short"})
        sh["usd_target"] = sh["w"] * aum_usd
        sh["shares"] = np.trunc(sh["usd_target"] / sh["px"])                         # whole shares toward zero: Alpaca does not short fractions
        sh["flags"] = np.where(sh["shares"] == 0, "below_one_share", "")
        out = pd.concat([out, sh], ignore_index=True)
    RES.mkdir(parents=True, exist_ok=True)
    out.to_csv(RES / f"{stem}.csv", index=False)
    info = {"score_column": a.score, "policy": a.policy, "policy_rules": pol, "source": src, "decision_month": month, "broker": BROKER, "selected": int(n_sel),
            "mapped_to_usd_lines": int(len(ok)), "weight_dropped_before_renormalisation": unmapped_share(sel, pol, col),
            "max_weight": float(ok["w"].max()), "effective_n": float(1 / (ok["w"] ** 2).sum()), "aum_usd": aum_usd,
            "flagged_below_min": int((ok["flags"] != "").sum()), "top10": out.head(10)[["ticker", "w"]].round(4).to_dict("records"), "overlay_state": trend_state(),
            "held_from": held_source, "held_long": len(held_long), "held_short": len(held_short), **short_info, "status": "dry run: no account read, no order endpoint"}
    (RES / f"{stem}.json").write_text(json.dumps(info, indent=1, default=float) + "\n")
    print(json.dumps({k: v for k, v in info.items() if k != "top10"}, indent=1, default=float))
    print(out.head(12).to_string(index=False))


if __name__ == "__main__":
    main()
