"""Agreement between a value rebuilt here and the engine's own value for the same stocks and month: the comparison behind every `*_replicates_engine`
gate of quant.features.ciqfill.assemble (the quarterly fundamentals family, each point-in-time family, insider, fails-to-deliver and EDGAR activity)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def equal_share(engine: pd.Series, rebuilt: pd.Series, both: pd.Series, dates: bool, atol: float, rtol: float = 1e-6) -> float | None:
    """Share of the rows selected by `both` on which the two agree, rounded to 4 decimals; None when `both` selects no row. Knowledge dates (`dates`)
    agree when they are the same day; values agree within `rtol` and `atol` (np.isclose)."""
    if not both.any():
        return None
    if dates:
        eq = float((pd.to_datetime(engine[both]) == pd.to_datetime(rebuilt[both])).mean())
    else:
        eq = float(np.isclose(engine[both].astype(float), rebuilt[both].astype(float), rtol=rtol, atol=atol).mean())
    return round(eq, 4)
