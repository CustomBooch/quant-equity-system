"""quant.features.ciqfill.features: the six Capital IQ fundamentals follow the engine formulas and never divide by a non-positive denominator."""
from __future__ import annotations

import numpy as np
import pytest

from quant.data.capiq.ciq import load_month
from quant.features.ciqfill.features import fund_features
from quant.features.ciqfill.tests.factories import SERIAL, company_rows, make_file


def test_features_follow_the_engine_formulas_and_guard_denominators(tmp_path):
    rows = company_rows(4)
    rows[1][7] = -5.0                                    # non-positive assets -> gp_at and leverage NaN
    rows[2][13] = 0.0                                    # zero market cap -> valuation ratios NaN
    rows[3][12] = SERIAL("2026-07-01")                   # late filing -> everything NaN
    t = load_month(make_file(tmp_path / "f.xlsx", rows)).table
    f = fund_features(t)
    r0 = rows[0]
    mc = r0[13] * 1e6
    assert f["gp_at"].iloc[0] == pytest.approx((r0[4] - r0[5]) / r0[7]) and f["bm"].iloc[0] == pytest.approx(r0[8] * 1e3 / mc)
    assert f["ep"].iloc[0] == pytest.approx(r0[11] * 1e3 / mc) and f["sp"].iloc[0] == pytest.approx(r0[4] * 1e3 / mc)
    assert f["cfp"].iloc[0] == pytest.approx(r0[9] * 1e3 / mc) and f["leverage"].iloc[0] == pytest.approx(r0[10] / r0[7])
    assert np.isnan(f.loc[1, ["gp_at", "leverage"]]).all() and np.isnan(f.loc[2, ["bm", "ep", "sp", "cfp"]]).all() and f.loc[3].isna().all()
    assert not np.isinf(f.to_numpy(dtype=float)).any()
