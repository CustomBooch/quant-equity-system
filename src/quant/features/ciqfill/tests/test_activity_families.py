"""quant.features.ciqfill.activity_families: insider and fails-to-deliver features follow the engine windows and coverage rules, computed by hand."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.features.ciqfill import activity_families as af


def test_insider_features_follow_the_engine_windows_and_coverage():
    ts = pd.Timestamp
    tx = pd.DataFrame({"permno": [1, 1, 1], "knowledge_date": [ts("2026-06-01"), ts("2026-03-01"), ts("2025-12-01")], "code": ["P", "S", "P"], "value": [100.0, 40.0, 10.0]})
    buyers = pd.DataFrame({"permno": [1, 1], "knowledge_date": [ts("2026-06-01"), ts("2026-05-01")], "owner_cik": [7, 7]})
    has_cik = pd.Series({1: True, 2: True, 3: False})
    r = af.insider_features(tx, buyers, (ts("2006-01-01"), ts("2026-09-30")), has_cik, pd.Series({1: 1e4, 2: 1e4, 3: 1e4}), ts("2026-07-01")).set_index("permno")
    a = r.loc[1]
    assert a["ins_net_value_6m"] == pytest.approx((100 - 40) / 1e4) and a["ins_n_buyers_6m"] == 1 and a["ins_buy_3m"] == 1.0 and a["ins_net_ratio_12m"] == pytest.approx(1 / 3)   # the Dec purchase is 212 days old: in the 12m ratio only
    b = r.loc[2]
    assert b["ins_net_value_6m"] == 0.0 and b["ins_n_buyers_6m"] == 0 and b["ins_buy_3m"] == 0.0 and np.isnan(b["ins_net_ratio_12m"])      # covered stock with no events: zero counts, no ratio
    assert r.loc[3].isna().all()                                                                                                          # no CIK link: unknown, not zero
    late = af.insider_features(tx, buyers, (ts("2006-01-01"), ts("2026-06-01")), has_cik, pd.Series({1: 1e4}), ts("2026-07-01"))
    assert late[af.INSIDER_COLS].isna().all().all()                                                                                       # filings end before the window: not covered
    early = af.insider_features(tx, buyers, (ts("2006-01-01"), ts("2026-09-30")), has_cik, pd.Series({1: 1e4}), ts("2026-03-01")).set_index("permno")
    assert early.loc[1, "ins_net_value_6m"] == pytest.approx(10 / 1e4)             # in March only the December purchase is known, and at 90 days it is inside the 182-day window


def test_ftd_uses_the_latest_known_month_and_leaves_months_without_a_state_empty():
    ts = pd.Timestamp
    ftd = pd.DataFrame({"permno": [1, 1, 2], "fmonth": [ts("2026-05-01"), ts("2026-04-01"), ts("2026-05-01")], "fails_mean": [10.0, 40.0, 5.0]})
    shares = {ts("2026-05-01"): pd.Series({1: 1000.0, 2: 0.0}), ts("2026-04-01"): pd.Series({1: 2000.0})}
    r = af.ftd_features(ftd, shares, (ts("2009-07-01"), ts("2026-08-01")), pd.Series([1, 2, 3]), ts("2026-07-01")).set_index("permno")
    assert r.loc[1, "ftd_frac"] == pytest.approx(0.01) and r.loc[1, "ftd_frac_chg"] == pytest.approx(0.01 - 0.02) and r.loc[1, "kd_ftd"] == ts("2026-06-30")
    assert r.loc[2, ["ftd_frac", "ftd_frac_chg", "kd_ftd"]].isna().all() and r.loc[3, ["ftd_frac", "ftd_frac_chg", "kd_ftd"]].isna().all()      # zero shares; not in the state
    assert af.ftd_features(ftd, shares, (ts("2009-07-01"), ts("2026-08-01")), pd.Series([1]), ts("2026-09-01"))[af.FTD_COLS].isna().all().all()  # July state does not exist
