"""Announcement-window features for bridge months from Yahoo daily bars, with the engine's windows and rules (panel/features/events.py).

Day 0 is the first trading day on or after the event date. For an event: ear = sum of abnormal returns (stock return minus market return) over trading days -1 to +1 (all three days must exist and have a
return); abn_vol = ln(mean volume over days -1..+1 / mean volume over days -60..-11), needing at least 30 volume days in the base window; knowledge date = trading day +1 plus one calendar day.
A decision month M takes the latest response whose knowledge date is before M and at most 120 days old. post_ann_car = cumulative abnormal return from day +2 to the last trading day before M.
  ear_rdq                                  events = Compustat report dates (exactly the engine's definition)
  days_since_ann                           decision date - report date of the response used; only where the I/B/E/S value is missing (October: the I/B/E/S surprise table has run out)
  ear_ann, abn_vol_ann, post_ann_car       the engine uses I/B/E/S announcement dates, which cannot be mapped to permnos after June; report dates are used instead: 97.5% of the engine's
                                           announcement events fall on the same day as a report date, 98.8% within a day (checked on 2025-01..2026-06). Parity is re-checked against the engine every run.
The market return is the proxy of quant.features.ciqfill.yahoo_price (market-equity weighted, 0.999 daily correlation with CRSP's value-weighted return).
"""
from __future__ import annotations

from typing import Any, cast

import numpy as np
import pandas as pd

ADMISSIBLE_DAYS = 120
BASE = (-60, -11)
MIN_BASE_DAYS = 30
COLS = ["ear_ann", "abn_vol_ann", "post_ann_car", "ear_rdq"]
FILL_IF_EMPTY = ["days_since_ann"]          # the engine takes it from the I/B/E/S surprise record; filled from report dates only where that is missing, never overwritten
KD_COLS = ["kd_ear_ann", "kd_ear_rdq"]

DailyArrays = dict[int, dict[str, np.ndarray]]          # permno -> its trading days: date, volume, abnormal return (ar) and cumulative abnormal return (car)


def _series(daily: pd.DataFrame, mkt: pd.Series) -> DailyArrays:
    """Per stock, in date order: the dates, volumes, abnormal returns (return minus the market proxy) and their running sum (missing counted as 0)."""
    d = daily.sort_values(["id", "date"]).copy()
    d["ret"] = d.groupby("id")["adjclose"].pct_change(fill_method=None)            # type: ignore[call-arg]  # pandas 2 keyword; pandas-stubs follow pandas 3
    d["ar"] = d["ret"] - d["date"].map(mkt)
    out: DailyArrays = {}
    for pid, g in d.groupby("id"):
        ar = g["ar"].to_numpy(float)
        out[int(cast(int, pid))] = {"date": g["date"].to_numpy("datetime64[ns]"), "vol": g["volume"].to_numpy(float), "ar": ar, "car": np.cumsum(np.nan_to_num(ar))}
    return out


def responses(daily: pd.DataFrame, mkt: pd.Series, events: pd.DataFrame, S: DailyArrays | None = None) -> pd.DataFrame:
    """`events`: permno, event_date. One row per (permno, day +1 date): event_date, idx0, day1_date, knowledge_date, ear, abn_vol, car_day1."""
    S = S if S is not None else _series(daily, mkt)
    rows: list[dict[str, Any]] = []
    for pid, ev in events.groupby("permno"):
        s = S.get(int(cast(int, pid)))
        if s is None:
            continue
        n = len(s["date"])
        for ed in pd.to_datetime(ev["event_date"]).drop_duplicates():
            i0 = int(np.searchsorted(s["date"], np.datetime64(ed), side="left"))
            if i0 - 1 < 0 or i0 + 1 >= n:                                       # needs trading days -1, 0 and +1
                continue
            w = s["ar"][i0 - 1:i0 + 2]
            base = s["vol"][max(i0 + BASE[0], 0):max(i0 + BASE[1] + 1, 0)]
            base = base[~np.isnan(base)]
            evt = s["vol"][i0 - 1:i0 + 2]
            v_evt = np.nanmean(evt) if (~np.isnan(evt)).any() else np.nan
            v_base = base.mean() if len(base) else np.nan
            rows.append({"permno": int(cast(int, pid)), "event_date": ed, "idx0": i0, "day1_date": pd.Timestamp(s["date"][i0 + 1]), "ear": float(w.sum()) if (~np.isnan(w)).all() else np.nan,
                         "abn_vol": float(np.log(v_evt / v_base)) if len(base) >= MIN_BASE_DAYS and v_base > 0 and v_evt > 0 else np.nan, "car_day1": float(s["car"][i0 + 1])})
    r = pd.DataFrame(rows, columns=["permno", "event_date", "idx0", "day1_date", "ear", "abn_vol", "car_day1", "knowledge_date"])
    if r.empty:
        return r
    r["knowledge_date"] = r["day1_date"] + pd.Timedelta(days=1)
    return r.sort_values(["permno", "day1_date", "event_date"]).drop_duplicates(["permno", "day1_date"], keep="last").reset_index(drop=True)


def _asof(resp: pd.DataFrame, permnos: pd.Series, month: pd.Timestamp) -> pd.DataFrame:
    """Per permno, the latest response known before `month` (`ok`: it exists and is at most ADMISSIBLE_DAYS old)."""
    r = resp[resp["knowledge_date"] < month].sort_values(["permno", "knowledge_date"]).drop_duplicates("permno", keep="last")
    j = pd.DataFrame({"permno": permnos.drop_duplicates().to_numpy()}).merge(r, on="permno", how="left")
    ok = j["knowledge_date"].notna() & ((month - j["knowledge_date"]).dt.days <= ADMISSIBLE_DAYS)
    return j.assign(ok=ok.to_numpy())


def event_features(daily: pd.DataFrame, mkt: pd.Series, events: pd.DataFrame, permnos: pd.Series, month: pd.Timestamp, S: DailyArrays | None = None,
                   resp: pd.DataFrame | None = None) -> pd.DataFrame:
    """The four features and their knowledge dates for decision `month`. `events`: permno, event_date (report dates; used for both families).

    `S` (from `_series(daily, mkt)`) and `resp` (from `responses(daily, mkt, events, S)`) do not depend on the month; a caller that builds several months from the
    same inputs passes them once (quant.features.ciqfill.assemble) instead of recomputing them for every month. The result is the same either way."""
    S = S if S is not None else _series(daily, mkt)
    resp = resp if resp is not None else responses(daily, mkt, events, S)
    a = _asof(resp, permnos, month) if not resp.empty else pd.DataFrame({"permno": permnos.drop_duplicates().to_numpy(), "ok": False})
    out = pd.DataFrame({"permno": a["permno"].to_numpy()})
    car_end: dict[int, float] = {}
    idx_end: dict[int, int] = {}
    for pid in out["permno"]:
        s = S.get(int(pid))
        if s is not None:
            k = int(np.searchsorted(s["date"], np.datetime64(month), side="left")) - 1          # last trading day before the decision month
            if k >= 0:
                car_end[pid], idx_end[pid] = s["car"][k], k
    ok = a["ok"].to_numpy(bool)
    for c in ("ear", "abn_vol", "car_day1", "idx0", "knowledge_date"):
        a[c] = a[c] if c in a else np.nan
    ce = out["permno"].map(car_end).to_numpy(float)
    ie = out["permno"].map(idx_end).to_numpy(float)
    out["ear_ann"] = np.where(ok, a["ear"], np.nan)
    out["abn_vol_ann"] = np.where(ok, a["abn_vol"], np.nan)
    out["post_ann_car"] = np.where(ok & (ie > a["idx0"].to_numpy(float) + 1), ce - a["car_day1"].to_numpy(float), np.nan)
    out["ear_rdq"] = out["ear_ann"]                                                                      # same events here (see the module note): the two families coincide
    out["days_since_ann"] = np.where(ok, (month - pd.to_datetime(a["event_date"])).dt.days, np.nan) if "event_date" in a else np.nan       # decision date - announcement (report) date
    out["kd_ear_ann"] = pd.to_datetime(a["knowledge_date"]).where(ok)
    out["kd_ear_rdq"] = out["kd_ear_ann"]
    return out
