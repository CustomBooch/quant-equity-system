"""BRIDGE forward archive and reconcile (SPEC.md amendment A2).

    python -m quant.data.free.archive snapshot [--limit N]   # archive today's free inputs for the investable universe (restartable)
    python -m quant.data.free.archive reconcile PATH_TO_LICENSED_FEATURES.parquet   # after a licensed pull

Archive layout: data/raw_ext/bridge_archive/<YYYY-MM-DD>/{analyst.parquet, prices.parquet, MANIFEST.json}. Personal-use Yahoo data; nothing here is published.
Reconcile compares archived-and-derived features with licensed-built features of the same (permno, month) using the acceptance rule of SPEC_S2.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import time
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

from quant.core.paths import REPO
from quant.core.release import sha256_file
from quant.core.release.release import utc_stamp

ARCHIVE = REPO / "data/raw_ext/bridge_archive"
MIN_COVERAGE, MIN_RANK_CORR = 0.70, 0.90
ANALYST_FEATURES = ["numest_fy1", "disp_fy1", "rev_fy1_1m", "rev_fy1_3m", "updown_fy1", "fy2_growth", "pos_surprise_4q", "surprise_sym", "surprise_disp"]


def latest_day(required: str) -> Path:
    """Newest archive day folder (data/raw_ext/bridge_archive/<YYYY-MM-DD>) that contains `required`."""
    days = sorted(p.name for p in ARCHIVE.iterdir() if (p / required).exists())
    if not days:
        raise FileNotFoundError(f"no archive day under {ARCHIVE} contains {required}")
    return ARCHIVE / days[-1]


def universe() -> pd.DataFrame:
    """The stocks to snapshot: supply your own universe (permno, ticker); the public edition has no default."""
    raise NotImplementedError("pass the universe to snapshot explicitly")


def analyst_row(tk: str) -> dict[str, Any]:
    """Current Yahoo consensus, revision and surprise fields for one ticker (network)."""
    import yfinance as yf

    t = yf.Ticker(tk)
    out: dict[str, Any] = {"ticker": tk}
    ee, tr, rv, eh = t.earnings_estimate, t.eps_trend, t.eps_revisions, t.earnings_history
    if ee is not None and "0y" in ee.index:
        y0 = ee.loc["0y"]
        y1 = ee.loc["+1y"] if "+1y" in ee.index else None
        out.update(numest_fy1=y0["numberOfAnalysts"], eps_avg_fy1=y0["avg"], eps_low_fy1=y0["low"], eps_high_fy1=y0["high"], eps_avg_fy2=(y1["avg"] if y1 is not None else np.nan))
    if tr is not None and "0y" in tr.index:
        r = tr.loc["0y"]
        out.update(eps_cur=r["current"], eps_30d=r["30daysAgo"], eps_90d=r["90daysAgo"])
    if rv is not None and "0y" in rv.index:
        r = rv.loc["0y"]
        out.update(up30=r["upLast30days"], down30=r["downLast30days"])
    try:                                                                         # revenue consensus: its history, once archived weekly, gives the sales-revision features (rev_sal_fy1_1m) and numest_chg_3m
        rv_ = t.revenue_estimate
        if rv_ is not None and "0y" in rv_.index:
            r0 = rv_.loc["0y"]
            out.update(rev_avg_fy1=r0.get("avg"), rev_num_fy1=r0.get("numberOfAnalysts"), rev_growth_fy1=r0.get("growth"))
    except Exception:  # noqa: BLE001, S110 - not every ticker has revenue estimates
        pass
    try:                                                                         # market value and shares: live inputs for the valuation ratios (reconciled at the next pull)
        fi = t.fast_info
        out.update(market_cap=float(fi["marketCap"]), shares=float(fi["shares"]), last_price=float(fi["lastPrice"]))
    except Exception:  # noqa: BLE001, S110 - some tickers have no fast_info
        pass
    if eh is not None and len(eh):
        e = eh.sort_index().tail(4)
        out.update(surp_last4=json.dumps([float(x) for x in e["surprisePercent"]]), last_quarter_end=str(e.index[-1])[:10], n_pos_surprise_4q=int((e["epsDifference"] > 0).sum()))
    return out


def analyst_features(s: pd.DataFrame) -> pd.DataFrame:
    """Features mirroring the licensed definitions as far as Yahoo allows (checked at the next pull)."""
    f = pd.DataFrame({"ticker": s["ticker"]})
    f["numest_fy1"] = s.get("numest_fy1")
    f["disp_fy1"] = (s["eps_high_fy1"] - s["eps_low_fy1"]) / (4.0 * s["eps_avg_fy1"].abs())
    f["rev_fy1_1m"] = s["eps_cur"] / s["eps_30d"] - 1
    f["rev_fy1_3m"] = s["eps_cur"] / s["eps_90d"] - 1
    f["updown_fy1"] = (s["up30"] - s["down30"]) / (s["up30"] + s["down30"]).replace(0, np.nan)
    f["fy2_growth"] = s["eps_avg_fy2"] / s["eps_avg_fy1"] - 1
    f["pos_surprise_4q"] = s.get("n_pos_surprise_4q")
    last = s["surp_last4"].map(lambda x: json.loads(x) if isinstance(x, str) else [])
    f["surprise_sym"] = last.map(lambda v: v[-1] if v else np.nan)
    f["surprise_disp"] = last.map(lambda v: float(np.std(v)) if len(v) >= 2 else np.nan)
    return f.replace([np.inf, -np.inf], np.nan)


def snapshot(limit: int | None = None, sleep: float = 0.35) -> Path:
    """Archive today's Yahoo analyst fields (restartable: tickers already in today's file are skipped) and two years of prices for the universe;
    returns the day folder. A ticker that fails three times is recorded with its error, not dropped."""
    day = dt.date.today().isoformat()
    out_dir = ARCHIVE / day
    out_dir.mkdir(parents=True, exist_ok=True)
    u = universe()
    if limit:
        u = u.head(limit)
    f = out_dir / "analyst.parquet"
    have = pd.read_parquet(f) if f.exists() else pd.DataFrame()
    done = set(have["ticker"]) if len(have) else set()
    rows = cast(list[dict[str, Any]], have.to_dict("records")) if len(have) else []
    for i, (pm, tk) in enumerate(zip(u["permno"], u["ticker"], strict=True)):
        if tk in done:
            continue
        for attempt in range(3):
            try:
                rows.append({**analyst_row(tk), "permno": int(pm), "snapshot_date": day})
                break
            except Exception as e:  # noqa: BLE001 - network/Yahoo errors are retried, then recorded
                time.sleep(2 * (attempt + 1))
                if attempt == 2:
                    rows.append({"ticker": tk, "permno": int(pm), "snapshot_date": day, "error": repr(e)[:80]})
        time.sleep(sleep)
        if i % 100 == 0:
            pd.DataFrame(rows).to_parquet(f, index=False)
            print("analyst", i, "of", len(u), flush=True)
    pd.DataFrame(rows).to_parquet(f, index=False)
    prices = out_dir / "prices.parquet"
    if not prices.exists():
        import yfinance as yf

        frames = []
        tks = list(u["ticker"])
        for i in range(0, len(tks), 100):
            chunk = tks[i : i + 100]
            try:
                px = yf.download(chunk, period="2y", auto_adjust=False, progress=False, group_by="ticker", threads=True)
            except Exception as e:  # noqa: BLE001
                print("price chunk failed", i, repr(e)[:80], flush=True)
                continue
            for tk in chunk:
                try:
                    x = px[tk].dropna(how="all").reset_index()
                except (KeyError, AttributeError):
                    continue
                x["ticker"] = tk
                frames.append(x)
            print("prices", min(i + 100, len(tks)), "of", len(tks), flush=True)
        if frames:
            pd.concat(frames, ignore_index=True).to_parquet(prices, index=False)
    manifest = {"snapshot_date": day, "created_utc": utc_stamp(), "source": "Yahoo Finance via yfinance (personal use)", "universe_size": int(len(u)),
                "files": {p.name: {"sha256": sha256_file(p), "bytes": p.stat().st_size} for p in (out_dir.glob("*.parquet"))}}
    (out_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return out_dir


def _rank_corr(g: pd.DataFrame, a: str, b: str) -> float:
    """Rank correlation of columns `a` and `b` within one group (Pearson correlation of the ranks)."""
    return float(g[a].rank().corr(g[b].rank()))


def monthly_rank_corr(pair: pd.DataFrame, a: str, b: str, month: str = "month") -> pd.Series:
    """Rank correlation of columns `a` and `b` within each `month` group; groups where it is undefined (one row, a constant column) are dropped."""
    return pair.groupby(month).apply(_rank_corr, a, b, include_groups=False).dropna()            # type: ignore[call-arg]  # pandas 2 keyword; pandas-stubs follow pandas 3


def reconcile_features(archived: pd.DataFrame, licensed: pd.DataFrame, features: list[str], by: tuple[str, str] = ("permno", "month")) -> dict[str, Any]:
    """Compare archived-and-derived with licensed-built features (SPEC_S2 acceptance rule). Both frames carry the `by` keys."""
    j = archived.merge(licensed, on=list(by), suffixes=("_free", "_lic"))
    res: dict[str, Any] = {"rows_joined": int(len(j)), "features": {}}
    for name in features:
        a, b = name + "_free", name + "_lic"
        if a not in j or b not in j:
            res["features"][name] = {"accepted": False, "reason": "missing column"}
            continue
        pair = j.dropna(subset=[a, b])
        cov = len(pair) / max(int(j[b].notna().sum()), 1)
        rc = monthly_rank_corr(pair, a, b, by[1]) if len(pair) else pd.Series(dtype=float)
        v: dict[str, Any] = {"coverage": float(cov), "n_pairs": int(len(pair)), "median_within_month_rank_corr": float(rc.median()) if len(rc) else None, "median_abs_diff": float((pair[a] - pair[b]).abs().median()) if len(pair) else None}
        v["accepted"] = bool(v["coverage"] >= MIN_COVERAGE and (v["median_within_month_rank_corr"] or 0) >= MIN_RANK_CORR)
        res["features"][name] = v
    res["fallback_features"] = [n for n, v in res["features"].items() if not v["accepted"]]
    return res


def main() -> None:
    """`snapshot [--limit N]` archives today's inputs; `reconcile PATH` compares every archived day with licensed-built features."""
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["snapshot", "reconcile"])
    ap.add_argument("path", nargs="?")
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()
    if a.cmd == "snapshot":
        print("archived to", snapshot(a.limit))
    else:
        lic = pd.read_parquet(a.path)
        days = sorted(ARCHIVE.glob("*/analyst.parquet"))
        arch = pd.concat([analyst_features(pd.read_parquet(p)).assign(permno=pd.read_parquet(p)["permno"], month=pd.Timestamp(p.parent.name).to_period("M").to_timestamp()) for p in days], ignore_index=True)
        res = reconcile_features(arch, lic, ANALYST_FEATURES)
        print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
