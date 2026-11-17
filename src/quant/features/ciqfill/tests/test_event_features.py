"""quant.features.ciqfill.event_features: announcement responses and their windows, checked against a hand computation."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.features.ciqfill import event_features as ef


def test_event_features_follow_the_engine_windows_by_hand():
    days = pd.bdate_range("2026-01-02", periods=100)
    rng = np.random.default_rng(2)
    r = rng.normal(0, 0.01, 100)
    r[0] = 0.0
    adj = 100 * np.cumprod(1 + r)
    vol = rng.uniform(1e5, 2e5, 100)
    daily = pd.DataFrame({"id": 1, "date": days, "close": adj, "adjclose": adj, "volume": vol})
    mkt = pd.Series(0.01 * np.sin(np.arange(100)), index=days)                      # a non-zero market so abnormal != raw return
    ar = np.r_[np.nan, adj[1:] / adj[:-1] - 1] - mkt.to_numpy()
    i0 = next(i for i in range(65, 90) if days[i].weekday() == 0)                   # a Monday: an event dated the Sunday before has day 0 = that Monday
    ev = pd.DataFrame({"permno": [1], "event_date": [days[i0] - pd.Timedelta(days=1)]})
    month = pd.Timestamp(days[i0 + 1].year, days[i0 + 1].month, 1) + pd.DateOffset(months=1)
    f = ef.event_features(daily, mkt, ev, pd.Series([1, 2]), month).set_index("permno")
    k = int(np.searchsorted(days.to_numpy(), np.datetime64(month), side="left")) - 1          # last trading day before the decision month
    assert f.loc[1, "ear_ann"] == pytest.approx(ar[i0 - 1:i0 + 2].sum())
    assert f.loc[1, "abn_vol_ann"] == pytest.approx(np.log(vol[i0 - 1:i0 + 2].mean() / vol[i0 - 60:i0 - 10].mean()))
    assert f.loc[1, "post_ann_car"] == pytest.approx(np.nansum(ar[i0 + 2:k + 1]))              # day +2 to the last trading day of the previous month
    assert f.loc[1, "kd_ear_ann"] == days[i0 + 1] + pd.Timedelta(days=1) and f.loc[1, "ear_rdq"] == f.loc[1, "ear_ann"]
    assert f.loc[2, ["ear_ann", "abn_vol_ann", "post_ann_car", "ear_rdq"]].isna().all()          # no events for this stock
    late = ef.event_features(daily, mkt, ev, pd.Series([1]), month + pd.DateOffset(months=5)).iloc[0]
    assert np.isnan(late["ear_ann"]) and np.isnan(late["post_ann_car"])                          # more than 120 days after the knowledge date
    edge = ef.responses(daily, mkt, pd.DataFrame({"permno": [1], "event_date": [days[99]]}))
    assert edge.empty                                                                           # no trading day +1 in the data: no response
    early = ef.responses(daily, mkt, pd.DataFrame({"permno": [1], "event_date": [days[5]]})).iloc[0]
    assert np.isnan(early["abn_vol"]) and not np.isnan(early["ear"])                             # fewer than 30 base-window volume days: no abnormal volume, the return response stands
    assert f.loc[1, "days_since_ann"] == (month - ev["event_date"].iloc[0]).days and np.isnan(f.loc[2, "days_since_ann"])     # decision date - report date of the response used
