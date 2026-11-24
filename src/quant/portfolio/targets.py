"""PAPER target book from the scores (dry run, no account or broker access). python -m quant.portfolio.targets [--aum-usd 100000]
The model's long book (quant.portfolio.model) from the newest scores, mapped to Alpaca symbols, shares at Yahoo's last price for the account value in USD, plus the
short leg (amendment A11) and both legs levered (A12). Writes lab/paper/results/target_book_<score>_<policy>_master_<month>_alpaca.csv and .json; the overlay state is reported.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from quant.features.ciqfill.yahoo_price import last_close
from quant.core.weights import apply_sector_cap, capped_weights, sic_division
from quant.portfolio import schedule as SCH
from quant.portfolio.plan import MIN_ORDER_USD, QTY_DECIMALS          # the order rules of SPEC section 4
from quant.core.paths import REPO, YAHOO_DAILY, latest_yahoo_bars
from quant.portfolio.broker import BROKER, SHORTS, line_column
from quant.portfolio.model import MODEL, leverage, long_policy, short_fraction

RES = REPO / "lab/paper/results"
POLICIES = {"vw90": {"entry": 0.90, "exit": 0.70, "cap": 0.10, "sizing": "vw"}, "example": long_policy()}          # example = the model's long book (quant.portfolio.model; SPEC A2/A3); vw90 is the v1 book, kept for the record
PRIMARY = "example"


def trend(px: pd.Series, month: str, today: dt.date | None = None) -> dict[str, Any]:
    """Overlay state for decision month `month` (YYYY-MM) from daily adjusted closes (SPEC A25): the close of M - 1 against the mean of the ten month-end closes
    to M - 1 gives the exposure (quant.portfolio.schedule.overlay_exposure). Refused before M has begun (M - 1 is not over) or when the bars do not reach M - 1."""
    today = today or dt.date.today()
    m = pd.Period(month, "M")
    if today < m.start_time.date():
        raise ValueError(f"refused: decision month {month} has not begun (today {today}): the close of {m - 1} is not known yet")
    me = px.groupby(cast(pd.DatetimeIndex, px.index).to_period("M")).last()
    me = me[me.index < m]
    if me.empty or me.index[-1] != m - 1:
        raise ValueError(f"refused: the market bars end in {me.index[-1] if len(me) else 'nothing'}, not {m - 1}: the overlay for {month} needs the close of {m - 1}")
    close, mean10 = float(me.iloc[-1]), float(me.rolling(10, min_periods=10).mean().iloc[-1])
    return {"last_complete_month": str(me.index[-1]), "close": close, "mean_10m": mean10, "below_trend": close < mean10, "exposure_now": SCH.overlay_exposure(close, mean10),
            "shadow_exposure_75": SCH.overlay_exposure(close, mean10, 0.75), "shadow_exposure_25": SCH.overlay_exposure(close, mean10, 0.25)}


def trend_state(month: str) -> dict[str, Any]:
    import yfinance as yf

    px = yf.download("VTI", period="2y", auto_adjust=False, progress=False)["Adj Close"].squeeze()
    px.index = pd.to_datetime(px.index).tz_localize(None)
    return trend(px, month)


def scores_master() -> tuple[pd.DataFrame, str, str]:
    """Amendments A4, A27: the newest quant.models.scores file (the traded score on the current master); market cap at the end of the previous month, the latest local Yahoo close as price."""
    from quant.models import scores as SC

    found = SC.latest()
    if found is None:
        raise SystemExit(f"refused: no score file in {SC.OUT}; run tools/py -m quant.models.scores first")
    month, f = found
    s = pd.read_parquet(f).set_index("permno")
    bars = latest_yahoo_bars(YAHOO_DAILY.parent) or YAHOO_DAILY
    px = {int(p): last_close(int(p), bars)[0] for p in s.index[s["q"] > min(v["entry"] for v in POLICIES.values())]}
    d = pd.DataFrame({"score": s["score"], "mcap": s["me_lag"], "px": pd.Series(px).reindex(s.index).fillna(s["price_lag"].abs())})
    return d, f"scores {f.name}", month[:7]


def short_lead(month: str, res: Path | None = None, ret: Callable[[int, str], float] | None = None, spy: Callable[[str], float] | None = None,
               months: int = MODEL.size.lookback_months, min_covered: float = 0.5) -> float | None:
    """SPEC A25: the live lead of the short-size rule (REGIME V2 W1): over the `months` complete months before `month`, the stored book's short basket
    (its weights, renormalised) less SPY, summed. Returns per name come from the newest monthly bars folder (SPEC A26); None (the A11 fallback) until every
    one of those months has a stored book with a short leg, or when bars cover less than `min_covered` of a basket's weight."""
    from quant.portfolio import shadow as SH  # type: ignore[attr-defined]  # not in the public edition: the example book is long only

    if ret is None:
        bars = latest_yahoo_bars()
        if bars is None:
            return None
        ret = lambda p, m: SH.bar_return(p, m, bars)                                    # noqa: E731
    res, spy = RES if res is None else res, spy or SH.spy_return
    total = 0.0
    for m in (str(pd.Period(month, "M") - i) for i in range(months, 0, -1)):
        f = res / f"{book_stem(m)}.csv"
        if not f.exists():
            return None
        b = pd.read_csv(f)
        s = b[b.get("side", pd.Series("long", index=b.index)) == "short"]
        if s.empty:
            return None
        w = s["w"].abs().to_numpy(float) / s["w"].abs().sum()
        r = np.array([ret(int(p), m) for p in s["permno"]], dtype=float)
        if w[np.isfinite(r)].sum() < min_covered:
            return None
        total += float(np.nansum(w * r)) - spy(m)                                        # a name without bars counts as 0, as in the shadow recorder
    return total


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
    s, rule = short_fraction(short_lead(month))
    info = {"short_fraction": s, "short_rule": rule, "short_candidates": n0, "short_names": int(len(b)), "short_weight_unmapped": unmapped,
            "short_weight_unshortable": unshortable, "expected_announcers_excluded": len(ann)}
    return b, info


def select(d: pd.DataFrame, pol: dict[str, Any], held: set[int] | None = None) -> pd.DataFrame:
    """Rows (indexed by permno) with positive market cap whose score percentile `q` is above the policy's entry level, or above its exit level for a
    name in `held` (last month's book): the holding rule of SPEC A3 and the backtest's policy. With nothing held this is the initial book."""
    keep = d.index.isin(list(held or set())) & (d["q"] > pol["exit"])
    out: pd.DataFrame = d[((d["q"] > pol["entry"]) | keep) & (d["mcap"] > 0)].copy()
    return out


def book_stem(month: str, policy: str = PRIMARY) -> str:
    """The target book's file stem for decision `month` (YYYY-MM): named after the score traded that month."""
    from quant.models import scores as SC

    return SC.book_stem(month, policy)


def previous_book(stem_prefix: str | tuple[str, ...], month: str, res: Path | None = None) -> tuple[set[int], set[int], str]:
    """(long permnos, short permnos, source) of the newest stored target book for an earlier decision month (files `<stem_prefix><YYYY-MM>_alpaca.csv`;
    several prefixes are searched together, so the first the model book holds what the last the model book held).
    A book without a permno column holds nothing, and the source says so (every stored book has one since 2026-10)."""
    import glob

    res = RES if res is None else res
    prefixes = (stem_prefix,) if isinstance(stem_prefix, str) else stem_prefix
    found = [(Path(f).stem.removeprefix(pre)[:7], f) for pre in prefixes for f in glob.glob(str(res / f"{pre}*_alpaca.csv"))]
    books = [f for m, f in sorted(found) if m < month]
    if not books:
        return set(), set(), "no earlier book: initial book (entry rule only)"
    b = pd.read_csv(books[-1])
    if "permno" not in b.columns:
        return set(), set(), f"{Path(books[-1]).name} has no permno column: holding rule not applied this month"
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


def sector_capped(ok: pd.DataFrame, d: pd.DataFrame, sic2: pd.Series, pol: dict[str, Any], month: str, aum_usd: float) -> pd.DataFrame:
    """Amendment A22: from decision month `sector_cap_from`, the long weights with each SIC division held to its share of the month's scored universe
    (`d`, market value) plus `sector_cap_extra`, jointly with the name cap; the backtest's rule (lab.portfolio.policy.apply_sector_cap). Earlier months and
    policies without the fields are returned unchanged."""
    if "sector_cap_from" not in pol or month < pol["sector_cap_from"]:
        return ok
    u = d[d["mcap"] > 0]
    usec = pd.Series(u["mcap"].to_numpy(float), index=sic_division(u.index.to_series().map(sic2))).groupby(level=0).sum()
    w = apply_sector_cap(ok["w"].to_numpy(float), sic_division(ok.index.to_series().map(sic2)), (usec / usec.sum()).to_dict(), pol["sector_cap_extra"],
                         max(pol["cap"], 1.0 / len(ok)))
    out = ok.assign(w=w)
    out["usd_target"] = out["w"] * aum_usd
    out["shares"] = np.round(out["usd_target"] / out["px"], QTY_DECIMALS)
    out["flags"] = np.where(out["usd_target"] < MIN_ORDER_USD, "below_min_order", "")
    return out.sort_values("w", ascending=False)


def map_alpaca(sel: pd.DataFrame) -> pd.DataFrame:
    """Adds cusip, ticker and the Alpaca symbol (`line`, None if there is none) to rows indexed by permno (amendment A10)."""
    from quant.data.instruments import alpaca as AM

    return sel.join(AM.map_permnos(sel.index)[["cusip", "ticker", "line"]])


def unmapped_share(sel: pd.DataFrame, pol: dict[str, Any], col: str = "line") -> float:
    """Share of the selected names' raw weight that has no broker line (`col` missing): the weight lost before renormalisation (preflight's 5% rail)."""
    w = raw_weight(sel["mcap"], pol)
    return float(w[sel[col].isna()].sum() / w.sum())


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", default=None, help="score name in the file name (default: the score traded that month, SPEC A27)")
    ap.add_argument("--aum-usd", type=float, default=100000.0, help="account value in USD (amendment A10: no FX)")
    ap.add_argument("--long-only", action="store_true", help="Alpaca book without the A11 short leg")
    ap.add_argument("--policy", default=PRIMARY, choices=list(POLICIES))
    a = ap.parse_args(argv)
    d, src, month = scores_master()
    from quant.models import scores as SC

    a.score = a.score or SC.traded_score(month)
    d["q"] = d["score"].rank(pct=True)
    pol = POLICIES[a.policy]
    col = line_column(BROKER)
    stem = f"target_book_{a.score}_{a.policy}_master_{month}_alpaca"
    if (RES / f"{stem}.csv").exists():
        raise SystemExit(f"refused: {RES / stem}.csv exists; a target book is the record its plans were made from: move it aside to rebuild it")
    held_long, held_short, held_source = previous_book(tuple(f"target_book_{n}_{a.policy}_master_" for n in ("EXAMPLE",)), month)
    sel = map_alpaca(select(d, pol, held_long))
    n_sel = len(sel)
    aum_usd = a.aum_usd
    if not sel[col].notna().any():
        raise SystemExit(f"refused: none of the {n_sel} selected names maps to an Alpaca symbol (is the asset list missing or stale?)")
    ok = size(sel[sel[col].notna()], pol, aum_usd)
    if "sector_cap_from" in pol and month >= pol["sector_cap_from"]:
        from quant.portfolio import shorts as S  # type: ignore[attr-defined]  # not in the public edition: the example book is long only

        sic2, _ = S.load_inputs(pd.Timestamp(f"{month}-01"), d.index)
        ok = sector_capped(ok, d, sic2, pol, month, aum_usd)
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
    overlay = trend_state(month)                                                    # before any file: a refused or failed overlay leaves no half-written book
    out.to_csv(RES / f"{stem}.csv", index=False)
    info = {"score_column": a.score, "policy": a.policy, "policy_rules": pol, "source": src, "decision_month": month, "broker": BROKER, "selected": int(n_sel),
            "mapped_to_usd_lines": int(len(ok)), "weight_dropped_before_renormalisation": unmapped_share(sel, pol, col),
            "max_weight": float(ok["w"].max()), "effective_n": float(1 / (ok["w"] ** 2).sum()), "aum_usd": aum_usd,
            "flagged_below_min": int((ok["flags"] != "").sum()), "top10": out.head(10)[["ticker", "w"]].round(4).to_dict("records"), "overlay_state": overlay,
            "held_from": held_source, "held_long": len(held_long), "held_short": len(held_short), **short_info, "status": "dry run: no account read, no order endpoint"}
    (RES / f"{stem}.json").write_text(json.dumps(info, indent=1, default=float) + "\n")
    print(json.dumps({k: v for k, v in info.items() if k != "top10"}, indent=1, default=float))
    print(out.head(12).to_string(index=False))


if __name__ == "__main__":
    main()
