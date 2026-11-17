"""quant.features.pricefeat: price features on known series."""
from __future__ import annotations

import numpy as np
import pandas as pd


def test_price_features_of_a_constant_growth_series_take_their_known_values():
    from quant.features import pricefeat as PF

    dates = pd.bdate_range("2025-01-01", "2026-03-31")
    px = pd.DataFrame({"id": 1, "date": dates, "close": 100.0 * 1.001 ** np.arange(len(dates)), "volume": 1000.0})
    px["adjclose"] = px["close"]
    mkt = pd.Series(0.001, index=dates)
    f = PF.features(PF.monthly_stats(px, mkt), pd.Series({1: 10000.0}), "2026-03-01").loc[1]
    assert abs(f["ret_1m"] - (1.001 ** 20 - 1)) < 0.01 and abs(f["vol_1m"]) < 1e-9 and f["zero_vol_frac_1m"] == 0
    assert abs(f["turnover_1m"] - 1000.0 * 20 / 10000.0) < 0.2 and abs(f["log_price"] - np.log(px["close"].iloc[-23])) < 0.2


def test_ret_36_13_needs_all_24_months():
    from quant.features import pricefeat as PF

    idx = pd.period_range("2023-01", "2026-02", freq="M")
    w = pd.DataFrame({1: 0.01, 2: 0.01}, index=idx)
    w.loc[pd.Period("2024-01", "M"), 2] = np.nan
    r = PF.ret_36_13_from_monthly(w, "2026-03-01")
    assert abs(r[1] - (1.01 ** 24 - 1)) < 1e-9 and np.isnan(r[2])


def test_log_me_uses_price_times_shares_and_constant_features_pass_the_distribution_check():
    AS = __import__("pytest").importorskip("quant.research.bridge.assemble", reason="the research layer is not part of the public edition")
    from quant.features import pricefeat as PF

    dates = pd.bdate_range("2025-01-01", "2026-03-31")
    px = pd.DataFrame({"id": 1, "date": dates, "close": 50.0, "volume": 100.0})
    px["adjclose"] = px["close"]
    f = PF.features(PF.monthly_stats(px, pd.Series(0.0, index=dates)), pd.Series({1: 2000.0}), "2026-03-01").loc[1]
    assert abs(f["log_me"] - np.log(100000.0)) < 1e-9
    z = pd.DataFrame({"x": np.zeros(50)})
    assert AS.distribution_check(z, z, ["x"])["ok"].all()
