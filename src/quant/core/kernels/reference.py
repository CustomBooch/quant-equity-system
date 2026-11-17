"""Python references of the kernels: the definitions the Rust kernels must match bit for bit, and the fallback when Rust is not in use.

design_all is lab.core.c5_experts.design_all (the C5 research copy, which live code cannot import without pulling in the research pipeline), built on the
canonical centred rank quant.core.stats.crank; src/quant/core/kernels/tests/test_reference.py proves it equals the original.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.core.stats import crank


def design_all(d: pd.DataFrame, cols: list[str]) -> np.ndarray:
    """Rows x 2k float32: within-month centred ranks of the k columns (missing -> 0), then a missing flag for every column."""
    g = d["month"]
    ranks = np.column_stack([crank(d[c].astype("float32"), g).fillna(0.0).to_numpy(np.float32) for c in cols])
    return np.hstack([ranks, d[cols].isna().to_numpy(np.float32)])
