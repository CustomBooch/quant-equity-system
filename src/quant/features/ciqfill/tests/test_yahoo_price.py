"""quant.features.ciqfill.yahoo_price: Yahoo chart files are read defensively, the market proxy is value weighted, and the parity rule judges each feature."""
from __future__ import annotations

import datetime as dt
import gzip

import numpy as np
import pandas as pd
import pytest

from quant.features.ciqfill import yahoo_price as yp
from quant.features.ciqfill.tests.factories import write_chart


def test_yahoo_loader_drops_empty_bars_duplicates_and_missing_files(tmp_path):
    write_chart(tmp_path / "7.json.gz", ["2026-01-02", "2026-01-05", "2026-01-05", "2026-01-06"], [10.0, None, 12.0, 13.0], adjclose=[10.0, 11.0, 12.0, 13.0])
    (tmp_path / "8.json.gz").write_bytes(gzip.compress(b"not json"))
    got = yp.load_yahoo([7, 8, 9], tmp_path)
    assert list(got["id"].unique()) == [7] and len(got) == 3 and got.loc[got["date"] == "2026-01-05", "close"].iloc[0] == 12.0      # empty close dropped, duplicate date keeps the last, a corrupt and a missing file are skipped


def test_last_close_skips_trailing_bars_without_a_close(tmp_path):
    write_chart(tmp_path / "7.json.gz", ["2026-10-20", "2026-10-21", "2026-10-22"], [10.0, 11.5, None])        # today's bar is open: no close yet
    assert yp.last_close(7, tmp_path) == (11.5, dt.date(2026, 10, 21))


def test_last_close_is_missing_without_a_file_or_any_close(tmp_path):
    write_chart(tmp_path / "7.json.gz", ["2026-10-20"], [None])
    for permno in (7, 8):
        px, day = yp.last_close(permno, tmp_path)
        assert np.isnan(px) and day is None


def test_market_proxy_is_market_equity_weighted_and_ignores_impossible_returns():
    days = pd.bdate_range("2026-02-02", periods=4)
    rows = []
    for i, (a, b) in enumerate(((100.0, 100.0), (110.0, 100.0), (110.0, 160.0), (110.0, 160.0))):
        rows += [{"id": 1, "date": days[i], "adjclose": a, "close": a, "volume": 1.0}, {"id": 2, "date": days[i], "adjclose": b, "close": b, "volume": 1.0}]
    me = {pd.Period("2026-01"): pd.Series({1: 3.0, 2: 1.0})}
    m = yp.market_proxy(pd.DataFrame(rows), me)
    assert m.iloc[1] == pytest.approx(0.75 * 0.10 + 0.25 * 0.0)                   # weights 3:1, returns +10% and 0%
    assert m.iloc[2] == pytest.approx(0.0)                                         # stock 2 jumps +60%: beyond the 50% clip, left out; stock 1 is flat
    assert np.isnan(m.iloc[0])                                                     # no return on the first day


def test_parity_rule_handles_near_constant_features_and_low_coverage():
    rng = np.random.default_rng(4)
    idx = pd.Index(range(200))
    eng = {"2026-05": pd.DataFrame({c: rng.normal(size=200) for c in yp.FEATURES}, index=idx)}
    eng["2026-05"]["zero_vol_frac_1m"] = 0.0
    built = {"2026-05": eng["2026-05"].copy()}
    r = yp.parity(built, eng)
    assert r["ret_1m"]["pass"] and r["zero_vol_frac_1m"]["pass"] and r["ret_1m"]["median_rank_corr"] == pytest.approx(1.0)
    built["2026-05"].loc[:15, "zero_vol_frac_1m"] = 0.3                              # 8% of a constant feature disagrees: fails the "99% equal" rule
    built["2026-05"].loc[:99, "vol_1m"] = np.nan                                      # half the engine rows have no Yahoo value: below the 70% coverage rule
    built["2026-05"]["ret_3m"] = -eng["2026-05"]["ret_3m"]                            # reversed ranks: correlation -1
    r = yp.parity(built, eng)
    assert not r["zero_vol_frac_1m"]["pass"] and not r["vol_1m"]["pass"] and not r["ret_3m"]["pass"] and r["vol_1m"]["coverage_of_engine_rows"] == pytest.approx(0.5)
