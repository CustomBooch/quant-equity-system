"""The six fundamentals that passed parity against the panel, with the engine's formulas (panel/features: fund family).

    gp_at    (TTM revenue - TTM cost of goods sold) / total assets            engine: (TTM revtq - TTM cogsq) / atq
    bm       book common equity / market equity at the pricing date           engine: ceqq / ME at M-1
    ep       TTM net income / market equity                                    engine: TTM ibq / ME
    sp       TTM revenue / market equity                                       engine: TTM saleq / ME
    cfp      TTM operating cash flow / market equity                           engine: TTM oancf / ME
    leverage total debt / total assets                                         engine: (dlcq + dlttq) / atq

Rules: a non-positive denominator gives NaN (never inf); a filing dated after the pricing date is not known at the decision, so its fundamentals are NaN (flagged by `late_filing`).
`ep` uses Net Income to the parent, which is close to but not identical with Compustat's income before extraordinary items (rank correlation 0.968 on the June 2026 panel month).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURES = ["gp_at", "bm", "ep", "sp", "cfp", "leverage"]


def _ratio(num: pd.Series, den: pd.Series) -> pd.Series:
    out = num / den.where(den > 0)
    return out.replace([np.inf, -np.inf], np.nan)


def fund_features(t: pd.DataFrame) -> pd.DataFrame:
    """Return the six features for every row of a CiqMonth table (index preserved)."""
    known = ~t["late_filing"]
    f = pd.DataFrame(index=t.index)
    f["gp_at"] = _ratio(t["revenue"] - t["cogs"], t["assets"])
    f["bm"] = _ratio(t["equity"], t["mcap"])
    f["ep"] = _ratio(t["net_income"], t["mcap"])
    f["sp"] = _ratio(t["revenue"], t["mcap"])
    f["cfp"] = _ratio(t["cfo"], t["mcap"])
    f["leverage"] = _ratio(t["debt"], t["assets"])
    return f.where(known)
