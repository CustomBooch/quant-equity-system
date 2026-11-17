"""quant.features.ciqfill.fill: engine cells are filled but never overwritten, universe flags follow the engine rule, and bridge rows take market data from the right source."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quant.data.capiq.ciq import load_month
from quant.features.ciqfill.features import FEATURES, fund_features
from quant.features.ciqfill.fill import bridge_rows, engine_flags, fill_month
from quant.data.capiq.ids import match_permno
from quant.features.ciqfill.tests.factories import company_rows, make_file, make_state


def test_fill_never_overwrites_and_flags_each_filled_cell():
    panel = pd.DataFrame({"permno": [1, 2, 3], "month": pd.Timestamp("2026-07-01"), **{c: [1.0, np.nan, np.nan] for c in FEATURES}})
    feats = pd.DataFrame({"permno": [1, 2], **{c: [9.0, 5.0] for c in FEATURES}})
    out, st = fill_month(panel, feats)
    assert out["gp_at"].tolist()[:2] == [1.0, 5.0] and np.isnan(out["gp_at"].iloc[2])      # row 1 kept its engine value; row 3 has no Capital IQ value
    assert out["gp_at_ciq"].tolist() == [False, True, False] and st["bm"] == {"engine_missing": 2, "filled": 1, "still_missing": 1}


def test_engine_flags_apply_the_engine_universe_rule():
    s = make_state(60)
    s.loc[0, "price"] = 3.0                                  # below $5: research but not investable
    s.loc[1, "pos_vol_days"] = 4                             # too few trading days
    s.loc[2, "shrtype"] = "AD"                               # not an ordinary share: outside research
    s.loc[3, "me"] = -1.0                                    # no market equity
    f = engine_flags(s)
    assert (~f.loc[[2, 3], "in_research"]).all() and f.loc[[0, 1], "in_research"].all() and not f.loc[[0, 1], "in_investable"].any()
    p20 = f.loc[f["in_research"] & (f["exch"] == "N"), "me"].quantile(0.2)
    ok = f["in_research"] & (f["price"] >= 5) & (f["me"] >= p20) & (f["pos_vol_days"] >= 10)
    assert (f["in_investable"] == ok).all()
    r = f.loc[f["in_research"] & (f["price"] >= 5)].sort_values(["me", "permno"], ascending=[False, True])
    assert r["me_rank"].tolist() == list(range(1, len(r) + 1)) and f.loc[~(f["in_research"] & (f["price"] >= 5)), "me_rank"].isna().all()


def test_bridge_rows_exact_state_versus_capital_iq_market_data(tmp_path):
    state = make_state(40)
    t = match_permno(load_month(make_file(tmp_path / "b.xlsx", company_rows(40, seed=10))).table, state)
    feats = fund_features(t)
    flags = engine_flags(state)
    exact = bridge_rows(t, feats, flags, pd.Timestamp("2026-07-01"), exact_state=True)
    assert len(exact) == 40 and exact["bridge"].all() and exact["is_holdout"].all() and exact["fwd_incomplete_1m"].all() and (exact["month"] == pd.Timestamp("2026-07-01")).all()
    assert (exact["bridge_market_source"] == "crsp_state").all() and exact["me_lag"].tolist() == state["me"].tolist()           # CRSP state values, not Capital IQ
    assert (exact["in_investable"].to_numpy() == flags["in_investable"].to_numpy()).all()
    assert (exact[[c + "_ciq" for c in FEATURES]].to_numpy() == exact[FEATURES].notna().to_numpy()).all() and (exact["sic2"] == 35).all()
    later = bridge_rows(t.iloc[:30], feats.iloc[:30], flags, pd.Timestamp("2026-08-01"), exact_state=False)
    assert len(later) == 30 and (later["bridge_market_source"] == "capital_iq").all()                                       # the ten stocks missing from the file are dropped
    assert later["me_lag"].tolist() == pytest.approx(t["mcap"].iloc[:30].tolist()) and later["log_me"].iloc[0] == pytest.approx(np.log(later["me_lag"].iloc[0]))
