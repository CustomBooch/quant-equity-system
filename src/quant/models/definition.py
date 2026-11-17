"""The model definition the scorer applies (quant.models.scores). This edition ships an EXAMPLE: replace this file with your own model.

The interface the scorer needs:
    FEATURES                      the panel columns the model reads (the release must carry them; the scorer checks their coverage)
    fit() -> (models, info)       fit or load the model; `info` is recorded beside the scores
    seed_ranks(models, frame)     seeds x rows: each fitted member's prediction, ranked (percentile) within each month of `frame`

The example is a transparent factor blend with no fitting: the mean of four cross-sectional percentile ranks, signed by the direction the academic
literature reports (12-2 momentum, profitability, low volatility, small size). It exists to show the plumbing end to end, not to make money.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

NAME = "EXAMPLE"
SIGNS = {"ret_12_2": 1.0, "roa": 1.0, "vol_3m": -1.0, "log_me": -1.0}       # feature -> direction of the expected return
FEATURES = list(SIGNS)


def fit() -> tuple[list[dict[str, float]], dict[str, Any]]:
    """Nothing to fit: one 'member' holding the signs."""
    return [dict(SIGNS)], {"model": NAME, "members": 1, "features": FEATURES}


def seed_ranks(models: list[dict[str, float]], frame: pd.DataFrame) -> np.ndarray:
    """Members x rows: the mean of the signed percentile ranks within each month (a missing input is left out of that row's mean)."""
    g = frame["month"]
    out = []
    for signs in models:
        ranks = pd.concat([(frame[c] * s).groupby(g).rank(pct=True) for c, s in signs.items()], axis=1)
        out.append(ranks.mean(axis=1, skipna=True).groupby(g).rank(pct=True).to_numpy())
    return np.vstack(out)
