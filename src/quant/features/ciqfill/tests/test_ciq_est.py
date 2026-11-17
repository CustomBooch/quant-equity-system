"""quant.features.ciqfill.ciq_est: Capital IQ consensus exports are read or refused, and the estimate features follow the engine definitions."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.data.capiq.ciq import CiqFormatError
from quant.features.ciqfill.ciq_est import EST_COLS, est_features, load_est
from quant.features.ciqfill.tests.factories import EST_MNEM, est_rows, make_est_file


def test_est_file_loads_and_wrong_layouts_are_refused(tmp_path):
    ok = load_est(make_est_file(tmp_path / "ok.xlsx", est_rows(10, 0, "2026-07-01")))
    assert ok.asof == pd.Timestamp("2026-07-01") and len(ok.table) == 10 and ok.table["same_day"].sum() == 1 and ok.table["cusip8"].iloc[0] == "C0000000"
    frac = est_rows(10, 0, "2026-07-01")
    for r in frac:
        r[10] += 0.4                                                                # real exports carry a time of day (46174.3968...): 2026-07-01 09:31 must not count as after 2026-07-01
    f = load_est(make_est_file(tmp_path / "frac.xlsx", frac))
    assert f.table["estdate"].max() == pd.Timestamp("2026-07-01") and f.table["same_day"].sum() == 1
    with pytest.raises(CiqFormatError, match="FY\+1"):
        load_est(make_est_file(tmp_path / "p.xlsx", est_rows(10, 0, "2026-07-01"), period="FY+2"))
    with pytest.raises(CiqFormatError, match="first of a month"):
        load_est(make_est_file(tmp_path / "d.xlsx", est_rows(10, 0, "2026-07-15"), asof="07/15/2026"))
    with pytest.raises(CiqFormatError, match="do not agree"):
        load_est(make_est_file(tmp_path / "o.xlsx", est_rows(10, 0, "2026-05-01"), asof="08/01/2026"))                # estimates three months older than the stated as-of date
    with pytest.raises(CiqFormatError, match="missing mnemonic"):
        load_est(make_est_file(tmp_path / "m.xlsx", est_rows(10, 0, "2026-07-01"), mnem=EST_MNEM[:9] + ["SP_EPS_WRONG"] + EST_MNEM[10:]))


def test_est_features_follow_the_engine_definitions():
    from quant.features.ciqfill.ciq_est import CiqEst
    ts = pd.Timestamp

    def est(asof, rows):
        df = pd.DataFrame(rows, columns=["cusip8", "mean", "std", "num", "estdate"])
        df["ticker_norm"] = df["cusip8"].str.replace("C", "T")
        df["age"] = (ts(asof) - df["estdate"]).dt.days
        return CiqEst(ts(asof), df.assign(med=df["mean"], high=df["mean"], low=df["mean"]), "x")
    state = pd.DataFrame({"permno": [1, 2, 3, 5], "cusip": ["C1", "C2", "C3", "C5"], "ticker": ["T1", "T2", "T3", "T5"], "me": [4.0, 3.0, 2.0, 1.0]})
    cur = est("2026-07-01", [["C1", 2.0, 0.2, 5, ts("2026-06-25")], ["C1", 9.0, 9.0, 2, ts("2026-06-30")], ["C2", 3.0, 0.3, 4, ts("2026-04-01")], ["C3", 0.0, 0.1, 3, ts("2026-06-20")],
                              ["C4", 1.0, 0.1, 3, ts("2026-06-20")], ["C5", 1.0, 0.1, 3, ts("2026-07-01")]])
    p1 = est("2026-06-01", [["C1", 1.6, 0.1, 5, ts("2026-05-20")], ["C3", 0.0, 0.1, 3, ts("2026-05-20")]])
    p3 = est("2026-04-01", [["C1", 4.0, 0.1, 8, ts("2026-03-20")]])
    f, stats = est_features(cur, p1, p3, state, ts("2026-07-01"), pd.Series([1, 2, 3, 5, 6]))
    f = f.set_index("permno")
    a = f.loc[1]
    assert a["numest_fy1"] == 5 and a["disp_fy1"] == pytest.approx(0.1) and a["rev_fy1_1m"] == pytest.approx(0.25) and a["rev_fy1_3m"] == pytest.approx(-0.5) and a["numest_chg_3m"] == -3 and not a["same_day"]   # duplicate company: the row with more estimates wins
    assert f.loc[2, EST_COLS].isna().all()                                         # newest estimate is 91 days old: outside the 45-day window, the whole family is empty
    assert f.loc[3, "numest_fy1"] == 3 and np.isnan(f.loc[3, "disp_fy1"]) and np.isnan(f.loc[3, "rev_fy1_1m"])          # zero mean: no ratio, never infinity
    assert f.loc[5, "numest_fy1"] == 3 and f.loc[5, "same_day"] and np.isnan(f.loc[5, "rev_fy1_1m"])                   # no earlier snapshot for this stock; estimate dated on the as-of day is flagged
    assert f.loc[6].drop("same_day").isna().all()                                  # permno 6 is not in the Capital IQ file
    assert stats["with_estimate"] == 6 and stats["match_rate"] == pytest.approx(5 / 6, abs=1e-3) and stats["fresh_stocks"] == 3       # C4 has an estimate but no matching stock
    none = est_features(cur, None, None, state, ts("2026-07-01"), pd.Series([1]))[0].iloc[0]
    assert np.isnan(none["rev_fy1_1m"]) and np.isnan(none["rev_fy1_3m"]) and none["numest_fy1"] == 5
