"""quant.data.free.archive: the reconcile rule between archived free features and licensed ones, and the Yahoo analyst feature definitions."""
from __future__ import annotations

import numpy as np
import pandas as pd
import json

from quant.data.free import archive as AR


def frames(n=300, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    months = pd.to_datetime(["2026-01-01", "2026-02-01"])
    rows = []
    for m in months:
        x = rng.normal(size=n)
        rows.append(pd.DataFrame({"permno": np.arange(n), "month": m, "f": x}))
    lic = pd.concat(rows, ignore_index=True)
    free = lic.copy()
    free["f"] = free["f"] + rng.normal(scale=noise, size=len(free)) if noise else free["f"]
    return free, lic


def test_reconcile_accepts_matching_features_and_rejects_noisy_ones():
    free, lic = frames(noise=0.0)
    ok = AR.reconcile_features(free, lic, ["f"])
    assert ok["features"]["f"]["accepted"] and ok["fallback_features"] == []
    free2, lic2 = frames(noise=5.0)
    bad = AR.reconcile_features(free2, lic2, ["f"])
    assert not bad["features"]["f"]["accepted"] and bad["fallback_features"] == ["f"]


def test_reconcile_rejects_low_coverage():
    free, lic = frames()
    free.loc[free.index % 2 == 0, "f"] = np.nan                                  # half the rows missing
    r = AR.reconcile_features(free, lic, ["f"])
    assert r["features"]["f"]["coverage"] < AR.MIN_COVERAGE and not r["features"]["f"]["accepted"]


def test_reconcile_reports_missing_columns_as_fallback():
    free, lic = frames()
    r = AR.reconcile_features(free, lic, ["f", "g"])
    assert "g" in r["fallback_features"]


def test_analyst_features_follow_the_licensed_definitions():
    s = pd.DataFrame({"ticker": ["A"], "numest_fy1": [10.0], "eps_high_fy1": [2.4], "eps_low_fy1": [1.6], "eps_avg_fy1": [2.0], "eps_avg_fy2": [2.5], "eps_cur": [2.0], "eps_30d": [1.9], "eps_90d": [1.6],
                      "up30": [6.0], "down30": [2.0], "n_pos_surprise_4q": [3], "surp_last4": [json.dumps([0.01, 0.02, -0.01, 0.03])]})
    f = AR.analyst_features(s).iloc[0]
    assert abs(f["disp_fy1"] - 0.1) < 1e-12 and abs(f["rev_fy1_3m"] - 0.25) < 1e-12 and abs(f["updown_fy1"] - 0.5) < 1e-12
    assert abs(f["surprise_sym"] - 0.03) < 1e-12 and f["pos_surprise_4q"] == 3 and f["surprise_disp"] > 0


def test_monthly_rank_corr_is_the_rank_correlation_per_month_and_drops_months_where_it_is_undefined():
    months = pd.to_datetime(["2026-01-01"] * 4 + ["2026-02-01"] * 4 + ["2026-03-01"])
    d = pd.DataFrame({"month": months, "a": [1.0, 2, 3, 4, 5, 5, 5, 5, 1], "b": [1.0, 4, 9, 16, 1, 2, 3, 4, 1]})   # monotone in January; constant in February; one row in March
    r = AR.monthly_rank_corr(d, "a", "b")
    assert r.index.tolist() == [pd.Timestamp("2026-01-01")] and r.iloc[0] == 1.0
