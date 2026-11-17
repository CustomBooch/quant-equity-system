"""The example model: signed percentile ranks within each month, averaged; a missing input is left out of that row's mean."""
import numpy as np
import pandas as pd

from quant.models import definition as D


def frame():
    return pd.DataFrame({"month": ["2026-01-01"] * 3 + ["2026-02-01"] * 2,
                         "ret_12_2": [0.3, 0.1, -0.2, 0.0, 0.5], "roa": [0.1, 0.0, -0.1, 0.2, np.nan],
                         "vol_3m": [0.1, 0.2, 0.3, 0.4, 0.1], "log_me": [5.0, 6.0, 7.0, 8.0, 4.0]})


def test_the_best_row_on_every_input_gets_the_top_rank_in_its_month():
    models, info = D.fit()
    r = D.seed_ranks(models, frame())
    assert r.shape == (1, 5) and info["features"] == D.FEATURES
    assert r[0][0] == 1.0 and r[0][2] == r[0][:3].min()                       # row 0: high momentum and profitability, low volatility, small
    assert r[0][4] == 1.0                                                      # February: better on the three inputs it has


def test_ranks_lie_in_zero_one_and_every_feature_has_a_sign():
    r = D.seed_ranks(*[D.fit()[0]], frame())
    assert ((r > 0) & (r <= 1)).all() and set(D.SIGNS.values()) <= {1.0, -1.0}
