"""Block bootstraps for monthly series.

Two families, both reproducing the repository's existing draws exactly for the same seed:
  moving block   non-circular blocks of fixed length, starts drawn with rng.choice (lab.core.c5_common, lab.core.divgics_common, rank_signals.core.metrics)
  stationary     Politis-Romano, geometric block lengths with mean `block`, one step at a time (lab.core.discovery_common, lab.portfolio.robust)
Use `stat` for any statistic of the resampled array; rows are time, so a 2-D array resamples all columns jointly (paired series).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import numpy as np
import numpy.typing as npt

Stat = Callable[[np.ndarray], Any]           # any statistic of the resampled array that converts to float (np.mean returns a NumPy scalar)


def moving_block_draws(v: np.ndarray, stat: Stat = np.mean, reps: int = 2000, block: int = 12, seed: int = 20261011) -> np.ndarray:
    """`reps` values of stat(v[idx]) with idx built from non-circular blocks of consecutive rows. Caller guarantees len(v) >= block."""
    n = len(v)
    rng = np.random.default_rng(seed)
    starts = np.arange(n - block + 1)
    k = int(np.ceil(n / block))
    out = np.empty(reps)
    for i in range(reps):
        idx = (rng.choice(starts, k)[:, None] + np.arange(block)).ravel()[:n]
        out[i] = stat(v[idx])
    return out


def moving_block_ci(x: npt.ArrayLike, stat: Stat = np.mean, reps: int = 2000, block: int = 12, seed: int = 20261011) -> list[float]:
    """95% percentile interval of stat under the moving-block bootstrap; NaN interval when the sample is shorter than two blocks."""
    v = np.asarray(x, dtype=float)
    v = v[~np.isnan(v)] if v.ndim == 1 else v
    if len(v) < 2 * block:
        return [float("nan"), float("nan")]
    d = moving_block_draws(v, stat, reps, block, seed)
    return [float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))]


def stationary_draws(v: np.ndarray, stat: Stat = np.mean, reps: int = 2000, block: int = 12, seed: int = 20261011) -> np.ndarray:
    """`reps` values of stat(v[idx]) under the stationary bootstrap (restart with probability 1/block, otherwise step to the next row, wrapping)."""
    n = len(v)
    rng = np.random.default_rng(seed)
    out = np.empty(reps)
    for b in range(reps):
        idx = np.empty(n, int)
        i = rng.integers(n)
        for t in range(n):
            if t > 0 and rng.random() < 1 / block:
                i = rng.integers(n)
            else:
                i = (i + 1) % n if t > 0 else i
            idx[t] = i
        out[b] = stat(v[idx])
    return out


def stationary_ci(x: npt.ArrayLike, stat: Stat = np.mean, reps: int = 2000, block: int = 12, seed: int = 20261011) -> list[float]:
    """95% percentile interval of stat under the stationary bootstrap."""
    v = np.asarray(x, dtype=float)
    v = v[~np.isnan(v)] if v.ndim == 1 else v
    d = stationary_draws(v, stat, reps, block, seed)
    return [float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))]
